import matplotlib
matplotlib.use('Agg')

import argparse, time, logging

import numpy as np
import mxnet as mx

from mxnet import gluon, nd
from mxnet import autograd as ag
from mxnet.gluon import nn
from mxnet.gluon.data.vision import transforms

from gluoncv.data.modelnet import pc_tranforms, modelnet_dataset
from gluoncv.model_zoo import get_model
from gluoncv.utils import makedirs, TrainingHistory
from gluoncv.data.shapenet import shapenet3d as gcv_shapenet


# CLI
parser = argparse.ArgumentParser(description='Train a model for point cloud classification.')
parser.add_argument('--batch-size', type=int, default=16,
                    help='training batch size per device (CPU/GPU).')
parser.add_argument('--num-gpus', type=int, default=3,
                    help='number of gpus to use.')
parser.add_argument('--model', type=str, default='pointnetcls',
                    help='model to use. options are pointnet and wrn. default is pointnet.')
parser.add_argument('-j', '--num-data-workers', dest='num_workers', default=16, type=int,
                    help='number of preprocessing workers')
parser.add_argument('--num-epochs', type=int, default=100,
                    help='number of training epochs.')
parser.add_argument('--lr', type=float, default=0.1,
                    help='learning rate. default is 0.1.')
parser.add_argument('--momentum', type=float, default=0.9,
                    help='momentum value for optimizer, default is 0.9.')
parser.add_argument('--wd', type=float, default=0.0001,
                    help='weight decay rate. default is 0.0001.')
parser.add_argument('--lr-decay', type=float, default=0.1,
                    help='decay rate of learning rate. default is 0.1.')
parser.add_argument('--lr-decay-period', type=int, default=0,
                    help='period in epoch for learning rate decays. default is 0 (has no effect).')
parser.add_argument('--lr-decay-epoch', type=str, default='40,60',
                    help='epoches at which learning rate decays. default is 40,60.')
parser.add_argument('--drop-rate', type=float, default=0.2,
                    help='dropout rate for wide pointnet. default is 0.')
parser.add_argument('--mode', type=str, default='hybrid',
                    help='mode in which to train the model. options are imperative, hybrid')
parser.add_argument('--save-period', type=int, default=10,
                    help='period in epoch of model saving.')
parser.add_argument('--save-dir', type=str, default='params',
                    help='directory of saved models')
parser.add_argument('--resume-from', type=str,
                    help='resume training from the model')
parser.add_argument('--save-plot-dir', type=str, default='.',
                    help='the path to save the history plot')
opt = parser.parse_args()

batch_size = opt.batch_size
classes = 16

num_gpus = opt.num_gpus
batch_size *= max(1, num_gpus)
context = [mx.gpu(i) for i in range(num_gpus)] if num_gpus > 0 else [mx.cpu()]
num_workers = opt.num_workers

lr_decay = opt.lr_decay
lr_decay_epoch = [int(i) for i in opt.lr_decay_epoch.split(',')] + [np.inf]

model_name = opt.model
if model_name.startswith('pointnetcls'):
    kwargs = {'classes': classes,
              'drop_rate': opt.drop_rate}
else:
    kwargs = {'classes': classes}
net = get_model(model_name, **kwargs)
if opt.resume_from:
    net.load_parameters(opt.resume_from, ctx = context)
optimizer = 'nag'

save_period = opt.save_period
if opt.save_dir and save_period:
    save_dir = opt.save_dir
    makedirs(save_dir)
else:
    save_dir = ''
    save_period = 0

plot_path = opt.save_plot_dir

logging.basicConfig(level=logging.INFO)
logging.info(opt)

transform_train = transforms.Compose([
    pc_tranforms.normalize_point_cloud,
    pc_tranforms.rotate_point_cloud,
    pc_tranforms.rotate_perturbation_point_cloud,
    pc_tranforms.random_point_dropout,
    pc_tranforms.random_scale_point_cloud,
    pc_tranforms.rotate_point_cloud_z,
    pc_tranforms.jitter_point_cloud
])

transform_test = transforms.Compose([
    pc_tranforms.normalize_point_cloud,
])

def test(ctx, val_data):
    metric = mx.metric.Accuracy()
    for i, batch in enumerate(val_data):
        data = gluon.utils.split_and_load(batch[0].transpose((0,2,1)), ctx_list=ctx, batch_axis=0)
        label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0)
        outputs = [net(X)[0] for X in data]
        metric.update(label, outputs)
    return metric.get()

def train(epochs, ctx):
    if isinstance(ctx, mx.Context):
        ctx = [ctx]
    net.initialize(mx.init.Xavier(), ctx=ctx)

    train_data = gluon.data.DataLoader(
        gcv_shapenet.PartDataset(train=True, classification = True, transform=transform_train),
        # modelnet_dataset.ModelNetDataset(modelnet='ModelNet40', train=True, npoint=2500, transform=transform_train),
        batch_size=batch_size, shuffle=True, last_batch='discard', num_workers=num_workers)

    val_data = gluon.data.DataLoader(
        gcv_shapenet.PartDataset(train=False, classification = True, transform=transform_test),
        # modelnet_dataset.ModelNetDataset(modelnet='ModelNet40', train=False, npoint=2500, transform=transform_test),
        batch_size=batch_size, shuffle=False, last_batch='discard', num_workers=num_workers)

    trainer = gluon.Trainer(net.collect_params(), optimizer,
                            {'learning_rate': opt.lr, 'wd': opt.wd, 'momentum': opt.momentum})
    metric = mx.metric.Accuracy()
    train_metric = mx.metric.Accuracy()
    loss_fn = gluon.loss.SoftmaxCrossEntropyLoss()
    train_history = TrainingHistory(['training-error', 'validation-error'])

    iteration = 0
    lr_decay_count = 0

    best_val_score = 0

    for epoch in range(epochs):
        tic = time.time()
        train_metric.reset()
        metric.reset()
        train_loss = 0
        num_batch = len(train_data)
        alpha = 1

        if epoch == lr_decay_epoch[lr_decay_count]:
            trainer.set_learning_rate(trainer.learning_rate*lr_decay)
            lr_decay_count += 1

        for i, batch in enumerate(train_data):
            data = gluon.utils.split_and_load(batch[0].transpose((0,2,1)), ctx_list=ctx, batch_axis=0)
            label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0)

            with ag.record():
                output = [net(X)[0] for X in data]
                loss = [loss_fn(yhat, y) for yhat, y in zip(output, label)]
            for l in loss:
                l.backward()
            trainer.step(batch_size)
            train_loss += sum([l.sum().asscalar() for l in loss])

            train_metric.update(label, output)
            name, acc = train_metric.get()
            iteration += 1

        train_loss /= batch_size * num_batch
        name, acc = train_metric.get()
        name, val_acc = test(ctx, val_data)
        train_history.update([1-acc, 1-val_acc])
        train_history.plot(save_path='%s/%s_shapenet_history.png'%(plot_path, model_name))

        if val_acc > best_val_score:
            best_val_score = val_acc
            net.save_parameters('%s/%.4f-shapenet-%s-%d-best.params'%(save_dir, best_val_score, model_name, epoch))

        name, val_acc = test(ctx, val_data)
        logging.info('[Epoch %d] train=%f val=%f loss=%f time: %f' %
            (epoch, acc, val_acc, train_loss, time.time()-tic))

        if save_period and save_dir and (epoch + 1) % save_period == 0:
            net.save_parameters('%s/shapenet-%s-%d.params'%(save_dir, model_name, epoch))

    if save_period and save_dir:
        net.save_parameters('%s/shapenet-%s-%d.params'%(save_dir, model_name, epochs-1))

def main():
    if opt.mode == 'hybrid':
        net.hybridize()
    train(opt.num_epochs, context)

if __name__ == '__main__':
    main()