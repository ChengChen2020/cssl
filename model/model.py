import torch
import torch.nn as nn
from torchvision.models import resnet
import torch.nn.functional as F

from functools import partial

class SplitBatchNorm(nn.BatchNorm2d):
	def __init__(self, num_features, num_splits, **kw):
		super().__init__(num_features, **kw)
		self.num_splits = num_splits

	def forward(self, input):
		N, C, H, W = input.shape
		if self.training or not self.track_running_stats:
			running_mean_split = self.running_mean.repeat(self.num_splits)
			running_var_split = self.running_var.repeat(self.num_splits)
			outcome = nn.functional.batch_norm(
				input.view(-1, C * self.num_splits, H, W), running_mean_split, running_var_split, 
				self.weight.repeat(self.num_splits), self.bias.repeat(self.num_splits),
				True, self.momentum, self.eps).view(N, C, H, W)
			self.running_mean.data.copy_(running_mean_split.view(self.num_splits, C).mean(dim=0))
			self.running_var.data.copy_(running_var_split.view(self.num_splits, C).mean(dim=0))
			return outcome
		else:
			return nn.functional.batch_norm(
				input, self.running_mean, self.running_var, 
				self.weight, self.bias, False, self.momentum, self.eps)

class ModelBasev1(nn.Module):
	def __init__(self, feature_dim=128, arch=None, bn_splits=8):
		super(ModelBasev1, self).__init__()

		norm_layer = partial(SplitBatchNorm, num_splits=bn_splits) if bn_splits > 1 else nn.BatchNorm2d

		self.f = []
		for name, module in getattr(resnet, arch)(num_classes=feature_dim, norm_layer=norm_layer).named_children():
			if name == 'conv1':
				module = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
			if not isinstance(module, nn.Linear) and not isinstance(module, nn.MaxPool2d):
				self.f.append(module)
		
		# encoder
		self.f = nn.Sequential(*self.f)
		# fc projector
		self.g = nn.Linear(512, feature_dim, bias=True)

	def forward(self, x):
		x = self.f(x)
		feature = torch.flatten(x, start_dim=1)
		out = self.g(feature)
		return F.normalize(out, dim=-1)

class MoCov1(nn.Module):
	def __init__(self, feature_dim=128, K=4096, m=0.99, T=0.1, arch='resnet18', bn_splits=8):
		super(MoCov1, self).__init__()

		self.K = K
		self.m = m
		self.T = T

		# create the encoders
		self.encoder_q = ModelBasev1(feature_dim=feature_dim, arch=arch, bn_splits=bn_splits)
		self.encoder_k = ModelBasev1(feature_dim=feature_dim, arch=arch, bn_splits=bn_splits)

		for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
			param_k.data.copy_(param_q.data)  # initialize
			param_k.requires_grad = False  # not update by gradient

		# create the queue
		self.register_buffer("queue", torch.randn(feature_dim, K))
		self.queue = nn.functional.normalize(self.queue, dim=0)

		self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

	@torch.no_grad()
	def _momentum_update_key_encoder(self):
		"""Momentum update of the key encoder"""
		for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
			param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

	@torch.no_grad()
	def _dequeue_and_enqueue(self, keys):
		batch_size = keys.shape[0]

		ptr = int(self.queue_ptr)
		assert self.K % batch_size == 0  # for simplicity

		# replace the keys at ptr (dequeue and enqueue)
		self.queue[:, ptr:ptr + batch_size] = keys.t()  # transpose
		ptr = (ptr + batch_size) % self.K  # move pointer

		self.queue_ptr[0] = ptr

	@torch.no_grad()
	def _batch_shuffle_single_gpu(self, x):
		"""Batch shuffle, for making use of BatchNorm."""
		# random shuffle index
		idx_shuffle = torch.randperm(x.shape[0]).cuda()
		# index for restoring
		idx_unshuffle = torch.argsort(idx_shuffle)

		return x[idx_shuffle], idx_unshuffle

	@torch.no_grad()
	def _batch_unshuffle_single_gpu(self, x, idx_unshuffle):
		"""Undo batch shuffle."""
		return x[idx_unshuffle]

	def contrastive_loss(self, im_q, im_k):
		# compute query features
		q = self.encoder_q(im_q)  # queries: NxC

		# compute key features
		with torch.no_grad():  # no gradient to keys
			# shuffle for making use of BN
			im_k_, idx_unshuffle = self._batch_shuffle_single_gpu(im_k)

			k = self.encoder_k(im_k_)  # keys: NxC

			# undo shuffle
			k = self._batch_unshuffle_single_gpu(k, idx_unshuffle)

		# compute logits
		# Einstein sum is more intuitive
		# positive logits: Nx1
		l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
		# negative logits: NxK
		l_neg = torch.einsum('nc,ck->nk', [q, self.queue.clone().detach()])

		# logits: Nx(1+K)
		logits = torch.cat([l_pos, l_neg], dim=1)

		# apply temperature
		logits /= self.T

		# labels: positive key indicators
		labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

		loss = nn.CrossEntropyLoss().cuda()(logits, labels)

		return loss, q, k

	def forward(self, im1, im2):

		with torch.no_grad():
			self._momentum_update_key_encoder()

		loss_12, q1, k2 = self.contrastive_loss(im1, im2)
		loss_21, q2, k1 = self.contrastive_loss(im2, im1)
		loss = loss_12 + loss_21
		k = torch.cat([k1, k2], dim=0)

		self._dequeue_and_enqueue(k)

		return loss

class ModelBasev2(nn.Module):
	def __init__(self, feature_dim=128, arch=None, bn_splits=8):
		super(ModelBasev2, self).__init__()

		norm_layer = partial(SplitBatchNorm, num_splits=bn_splits) if bn_splits > 1 else nn.BatchNorm2d

		self.f = []
		for name, module in getattr(resnet, arch)(num_classes=feature_dim, norm_layer=norm_layer).named_children():
			if name == 'conv1':
				module = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
			if not isinstance(module, nn.Linear) and not isinstance(module, nn.MaxPool2d):
				self.f.append(module)
		
		self.f = nn.Sequential(*self.f)
		self.g = nn.Sequential(
							nn.Linear(512, 1024, bias=False),
							nn.BatchNorm1d(1024),
							nn.ReLU(inplace=True),
							nn.Dropout(0.3),
							nn.Linear(1024, feature_dim, bias=True)
						)

	def forward(self, x):
		x = self.f(x)
		feature = torch.flatten(x, start_dim=1)
		out = self.g(feature)
		return F.normalize(out, dim=-1)

class MoCov2(nn.Module):
	def __init__(self, feature_dim=128, K=4096, m=0.99, T=0.1, arch='resnet50', bn_splits=8):
		super(MoCov2, self).__init__()

		self.K = K
		self.m = m
		self.T = T

		# create the encoders
		self.encoder_q = ModelBasev2(feature_dim=feature_dim, arch=arch, bn_splits=bn_splits)
		self.encoder_k = ModelBasev2(feature_dim=feature_dim, arch=arch, bn_splits=bn_splits)

		for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
			param_k.data.copy_(param_q.data)  # initialize
			param_k.requires_grad = False  # not update by gradient

		# create the queue
		self.register_buffer("queue", torch.randn(feature_dim, K))
		self.queue = nn.functional.normalize(self.queue, dim=0)

		self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

	@torch.no_grad()
	def _momentum_update_key_encoder(self):
		"""Momentum update of the key encoder"""
		for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
			param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

	@torch.no_grad()
	def _dequeue_and_enqueue(self, keys):
		batch_size = keys.shape[0]

		ptr = int(self.queue_ptr)
		assert self.K % batch_size == 0  # for simplicity

		# replace the keys at ptr (dequeue and enqueue)
		self.queue[:, ptr:ptr + batch_size] = keys.t()  # transpose
		ptr = (ptr + batch_size) % self.K  # move pointer

		self.queue_ptr[0] = ptr

	@torch.no_grad()
	def _batch_shuffle_single_gpu(self, x):
		"""Batch shuffle, for making use of BatchNorm."""
		# random shuffle index
		idx_shuffle = torch.randperm(x.shape[0]).cuda()

		# index for restoring
		idx_unshuffle = torch.argsort(idx_shuffle)

		return x[idx_shuffle], idx_unshuffle

	@torch.no_grad()
	def _batch_unshuffle_single_gpu(self, x, idx_unshuffle):
		"""Undo batch shuffle."""
		return x[idx_unshuffle]

	def contrastive_loss(self, im_q, im_k):
		# compute query features
		q = self.encoder_q(im_q)  # queries: NxC

		# compute key features
		with torch.no_grad():  # no gradient to keys
			# shuffle for making use of BN
			im_k_, idx_unshuffle = self._batch_shuffle_single_gpu(im_k)

			k = self.encoder_k(im_k_)  # keys: NxC

			# undo shuffle
			k = self._batch_unshuffle_single_gpu(k, idx_unshuffle)

		# compute logits
		# Einstein sum is more intuitive
		# positive logits: Nx1
		l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
		# negative logits: NxK
		l_neg = torch.einsum('nc,ck->nk', [q, self.queue.clone().detach()])

		# logits: Nx(1+K)
		logits = torch.cat([l_pos, l_neg], dim=1)

		# apply temperature
		logits /= self.T

		# labels: positive key indicators
		labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

		loss = nn.CrossEntropyLoss().cuda()(logits, labels)

		return loss, q, k

	def forward(self, im1, im2):

		with torch.no_grad():  # no gradient to keys
			self._momentum_update_key_encoder()

		loss_12, q1, k2 = self.contrastive_loss(im1, im2)
		loss_21, q2, k1 = self.contrastive_loss(im2, im1)
		loss = loss_12 + loss_21
		k = torch.cat([k1, k2], dim=0)

		self._dequeue_and_enqueue(k)

		return loss

class SimCLRv1(nn.Module):
	def __init__(self, feature_dim=128, arch='resnet50'):
		super(SimCLRv1, self).__init__()

		self.f = []
		for name, module in getattr(resnet, arch)().named_children():
			if name == 'conv1':
				module = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
			if not isinstance(module, nn.Linear) and not isinstance(module, nn.MaxPool2d):
				self.f.append(module)
		# encoder
		self.f = nn.Sequential(*self.f)
		# projection head
		self.g = nn.Sequential(
							nn.Linear(2048, 512, bias=False),
							nn.BatchNorm1d(512),
							nn.ReLU(inplace=True), 
							nn.Linear(512, feature_dim, bias=True)
						)

	def forward(self, x):
		x = self.f(x)
		feature = torch.flatten(x, start_dim=1)
		out = self.g(feature)
		return F.normalize(feature, dim=-1), F.normalize(out, dim=-1)

class SimCLRv2(nn.Module):
	def __init__(self, feature_dim=128, arch='resnet50'):
		super(SimCLRv2, self).__init__()

		self.f = []
		for name, module in getattr(resnet, arch)().named_children():
			if name == 'conv1':
				module = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
			if not isinstance(module, nn.Linear) and not isinstance(module, nn.MaxPool2d):
				self.f.append(module)
		# encoder
		self.f = nn.Sequential(*self.f)
		# projection head
		self.g1 = nn.Sequential(
							nn.Linear(2048, 1024, bias=False),
							nn.BatchNorm1d(1024),
							nn.ReLU(inplace=True),
							nn.Dropout(0.3),
						)
		self.g2 = nn.Sequential(
							nn.Linear(1024, 512, bias=False),
							nn.BatchNorm1d(512),
							nn.ReLU(inplace=True),
							nn.Linear(512, feature_dim, bias=True)
						)

	def forward(self, x):
		x = self.f(x)
		feature = torch.flatten(x, start_dim=1)
		out = self.g2(self.g1(feature))
		return F.normalize(feature, dim=-1), F.normalize(out, dim=-1)

