''''
train model
'''
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
from configurations.modelConfig import params, num_classes
from tqdm import tqdm
import numpy as np
from utils.visualizations import plot_confusion_matrix
from utils.metrics import updateConfusionMatrix, calculateF1Score
from tensorboardX import SummaryWriter
from utils.save import saveModelandMetrics
from torch.optim.lr_scheduler import MultiStepLR
from utils.visualizations import plotROC

class Trainer(object):
	def __init__(self, model, train_loader, valid_loader, expt_folder):
		super(Trainer, self).__init__()
		
		if torch.cuda.is_available():
			self.model = model.cuda()
			
		self.train_loader = train_loader
		self.valid_loader = valid_loader
		self.optimizer = torch.optim.Adam(model.parameters(),
										  lr=params['train']['learning_rate'])
		self.classification_criterion = nn.NLLLoss(weight=torch.FloatTensor(params['train']['label_weights']).cuda())
		#self.autoencoder_criterion = nn.MSELoss()
		
		self.curr_epoch = 0
		self.batchstep = 0
		
		self.expt_folder = expt_folder
		self.writer = SummaryWriter(log_dir=expt_folder)
		
		self.train_losses_class, self.train_losses_reconst, self.train_losses_kld, self.valid_losses, \
		self.train_f1_Score, \
		self.valid_f1_Score,\
		self.train_accuracy, self.valid_accuracy = ([] for i in range(8))
		
		#self.lambda_ = 1	#hyper-parameter to control regularizer by reconstruction loss
	def vae_loss(self, recon_x, x, mu, logvar):
		BCE = F.binary_cross_entropy(recon_x, x, size_average=False)
		# BCE = F.mse_loss(recon_x, x, size_average=False)
		
		KLD = -0.5 * torch.mean(1 + logvar - mu.pow(2) - logvar.exp())
		
		return BCE + KLD, BCE, KLD
	
		
	
	def train(self):
		scheduler = MultiStepLR(self.optimizer, milestones=params['train']['lr_schedule'], gamma=0.1)	# [40,
		# 60] earlier
		
		for _ in range(params['train']['num_epochs']):
			print('Training...\nEpoch : '+str(self.curr_epoch))
			scheduler.step()
			
			# Train Model
			accuracy, classification_loss, reconstruction_loss, f1_score = self.trainEpoch()
			
			self.train_losses_class.append(classification_loss)
			self.train_losses_reconst.append(reconstruction_loss)
			self.train_accuracy.append(accuracy)
			self.train_f1_Score.append(f1_score)
			
			# Validate Model
			print ('Validation...')
			self.validate()
			
			# Save model
			saveModelandMetrics(self)
			
			# TODO : Stop learning if model doesn't improve for 10 epochs --> Save best model
			
			self.curr_epoch += 1
	
	def trainEpoch(self):
		self.model.train(True)
		
		pbt = tqdm(total=len(self.train_loader))
		
		cm = np.zeros((num_classes, num_classes), int)
		
		minibatch_losses_class = 0
		minibatch_losses_reconst = 0
		minibatch_accuracy = 0
		
		for batch_idx, (images, labels) in enumerate(self.train_loader):
			
			torch.cuda.empty_cache()
			accuracy, class_loss, conf_mat, reconst_loss = self.trainBatch(batch_idx, images, labels)
			
			minibatch_losses_class += class_loss
			minibatch_losses_reconst += reconst_loss
			minibatch_accuracy += accuracy
			cm += conf_mat
			
			pbt.update(1)
		
		pbt.close()
		
		minibatch_losses_class /= len(self.train_loader)
		minibatch_losses_reconst /= len(self.train_loader)
		minibatch_accuracy /= len(self.train_loader)
		
		# Plot losses
		self.writer.add_scalar('train_classification_loss', minibatch_losses_class , self.curr_epoch)
		self.writer.add_scalar('train_reconstruction_loss', minibatch_losses_reconst, self.curr_epoch)
		self.writer.add_scalar('train_accuracy', minibatch_accuracy, self.curr_epoch)
		
		# Plot confusion matrices
		plot_confusion_matrix(cm, location=self.expt_folder, title='Confusion matrix, ' \
																			  '(Train)')
		
		# F1 Score
		f1_score = calculateF1Score(cm)
		self.writer.add_scalar('train_f1_score', f1_score, self.curr_epoch)
		print('F1 Score : ', f1_score)
		
		# plot ROC curve
		#plotROC(cm, location=self.expt_folder, title='ROC Curve(Train)')
		
		return minibatch_accuracy, minibatch_losses_class, minibatch_losses_reconst, f1_score
	
	def trainBatch(self, batch_idx, images, labels):
		images = Variable(images).cuda()
		labels = Variable(labels).cuda()
		labels = labels.view(-1, )
		
		# Forward + Backward + Optimize
		
		# x_hat is reconstructed image, p_hat is predicted classification probability
		z, mu, logvar, x_hat, p_hat = self.model(images)
		classification_loss = self.classification_criterion(p_hat, labels)
		vae_loss, bce, kld = self.vae_loss(x_hat, images, mu, logvar)
		
		loss = classification_loss + params['train']['lambda'] * vae_loss
		
		self.optimizer.zero_grad()
		#classification_loss.backward(retain_graph=True)
		loss.backward()
		
		
		self.optimizer.step()
		
		# Compute accuracy
		_, pred_labels = torch.max(p_hat, 1)
		accuracy = (labels == pred_labels).float().mean()
		
		# Print metrics
		if batch_idx % 100 == 0:
			print('Epoch [%d/%d], Batch [%d/%d] Classification Loss: %.4f Accuracy: %0.2f Reconstruction Loss: %.4f'
				  % (self.curr_epoch, params['train']['num_epochs'],
					 batch_idx,
					 len(self.train_loader),
					 classification_loss.data[0],
					 accuracy,
				  vae_loss.data[0]))
			
		cm = updateConfusionMatrix(labels.data.cpu().numpy(), pred_labels.data.cpu().numpy())
		
		# clean GPU
		del images, labels, x_hat, p_hat, _, pred_labels
		
		self.writer.add_scalar('minibatch_classification_loss', np.mean(classification_loss.data[0]), self.batchstep)
		self.writer.add_scalar('minibatch_reconstruction_loss', np.mean(reconstruction_loss.data[0]), self.batchstep)
		self.batchstep += 1
		
		return accuracy, classification_loss.data[0], cm, reconstruction_loss.data[0]
	
	def validate(self):
		self.model.eval()
		correct = 0
		cm = np.zeros((num_classes, num_classes), int)
		
		pb = tqdm(total=len(self.valid_loader))
		
		for i, (images, labels) in enumerate(self.valid_loader):
			img = Variable(images, volatile=True).cuda()
			_, outputs = self.model(img)
			_, predicted = torch.max(outputs.data, 1)
			labels = labels.view(-1, )
			correct += ((predicted.cpu() == labels).float().mean())
			
			cm += updateConfusionMatrix(labels.numpy(), predicted.cpu().numpy())
			
			loss = self.classification_criterion(outputs, Variable(labels).cuda())
			self.valid_losses.append(loss.data[0])
			
			del img
			pb.update(1)
			
		pb.close()
		
		correct /= len(self.valid_loader)
		
		print('Validation Accuracy : %0.6f' % correct)
		
		self.valid_accuracy.append(correct)
		
		# Plot loss and accuracy
		self.writer.add_scalar('validation_accuracy', correct, self.curr_epoch)
		self.writer.add_scalar('validation_loss', np.mean(self.valid_losses), self.curr_epoch)
		
		# Plot confusion matrices
		plot_confusion_matrix(cm, location=self.expt_folder, title='Confusion matrix, ' \
																			  'without normalization (Valid)')
		
		# F1 Score
		f1_score = calculateF1Score(cm)
		self.writer.add_scalar('valid_f1_score', f1_score, self.curr_epoch)
		print('F1 Score : ', calculateF1Score(cm))
		self.valid_f1_Score.append(f1_score)
		
		# plot ROC curve
		#plotROC(cm, location=self.expt_folder, title='ROC Curve(Valid)')
	
	def test(self, test_loader):
		self.model.eval()
		print ('Test...')
		
		correct =0
		test_losses = 0
		cm = np.zeros((num_classes, num_classes), int)
		
		pb = tqdm(total=len(self.valid_loader))
		
		for i, (images, labels) in enumerate(test_loader):
			img = Variable(images, volatile=True).cuda()
			_, outputs = self.model(img)
			_, predicted = torch.max(outputs.data, 1)
			labels = labels.view(-1, )
			correct += ((predicted.cpu() == labels).float().mean())
			
			cm += updateConfusionMatrix(labels.numpy(), predicted.cpu().numpy())
			
			loss = self.classification_criterion(outputs, Variable(labels).cuda())
			test_losses += loss.data[0]
			
			del img
			pb.update(1)
		
		pb.close()
		
		correct /= len(test_loader)
		test_losses /= len(test_loader)
		
		print('Test Accuracy : %0.6f' % correct)
		print('Test Losses : %0.6f' % test_losses)
		
		# Plot confusion matrices
		plot_confusion_matrix(cm, location=self.expt_folder, title='Confusion matrix, ' \
																			  'without normalization (Test)')
		
		# F1 Score
		print('F1 Score : ', calculateF1Score(cm))
		
		# plot ROC curve
		#plotROC(cm, location=self.expt_folder, title='ROC Curve(Test)')
		