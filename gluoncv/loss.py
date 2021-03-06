# pylint: disable=arguments-differ
"""Custom losses.
Losses are subclasses of gluon.loss.Loss which is a HybridBlock actually.
"""
from __future__ import absolute_import
from mxnet import gluon, autograd
from mxnet import nd
from mxnet.gluon.loss import Loss, _apply_weighting, _reshape_like
from .nn.coder import NormalizedBoxCenterDecoder

__all__ = ['FocalLoss', 'SSDMultiBoxLoss', 'YOLOV3Loss', 'YOLACTMultiBoxLoss',
           'MixSoftmaxCrossEntropyLoss', 'MixSoftmaxCrossEntropyOHEMLoss',
           'DistillationSoftmaxCrossEntropyLoss', 'MaskLoss', 'MaskFCOSLoss']

class FocalLoss(Loss):
    """Focal Loss for inbalanced classification.
    Focal loss was described in https://arxiv.org/abs/1708.02002

    Parameters
    ----------
    axis : int, default -1
        The axis to sum over when computing softmax and entropy.
    alpha : float, default 0.25
        The alpha which controls loss curve.
    gamma : float, default 2
        The gamma which controls loss curve.
    sparse_label : bool, default True
        Whether label is an integer array instead of probability distribution.
    from_logits : bool, default False
        Whether input is a log probability (usually from log_softmax) instead.
    batch_axis : int, default 0
        The axis that represents mini-batch.
    weight : float or None
        Global scalar weight for loss.
    num_class : int
        Number of classification categories. It is required is `sparse_label` is `True`.
    eps : float
        Eps to avoid numerical issue.
    size_average : bool, default True
        If `True`, will take mean of the output loss on every axis except `batch_axis`.

    Inputs:
        - **pred**: the prediction tensor, where the `batch_axis` dimension
          ranges over batch size and `axis` dimension ranges over the number
          of classes.
        - **label**: the truth tensor. When `sparse_label` is True, `label`'s
          shape should be `pred`'s shape with the `axis` dimension removed.
          i.e. for `pred` with shape (1,2,3,4) and `axis = 2`, `label`'s shape
          should be (1,2,4) and values should be integers between 0 and 2. If
          `sparse_label` is False, `label`'s shape must be the same as `pred`
          and values should be floats in the range `[0, 1]`.
        - **sample_weight**: element-wise weighting tensor. Must be broadcastable
          to the same shape as label. For example, if label has shape (64, 10)
          and you want to weigh each sample in the batch separately,
          sample_weight should have shape (64, 1).
    Outputs:
        - **loss**: loss tensor with shape (batch_size,). Dimensions other than
          batch_axis are averaged out.
    """
    def __init__(self, axis=-1, alpha=0.25, gamma=2, sparse_label=True,
                 from_logits=False, batch_axis=0, weight=None, num_class=None,
                 eps=1e-12, size_average=True, **kwargs):
        super(FocalLoss, self).__init__(weight, batch_axis, **kwargs)
        self._axis = axis
        self._alpha = alpha
        self._gamma = gamma
        self._sparse_label = sparse_label
        if sparse_label and (not isinstance(num_class, int) or (num_class < 1)):
            raise ValueError("Number of class > 0 must be provided if sparse label is used.")
        self._num_class = num_class
        self._from_logits = from_logits
        self._eps = eps
        self._size_average = size_average

    def hybrid_forward(self, F, pred, label, sample_weight=None):
        """Loss forward"""
        if not self._from_logits:
            pred = F.sigmoid(pred)
        if self._sparse_label:
            one_hot = F.one_hot(label, self._num_class)
        else:
            one_hot = label > 0
        pt = F.where(one_hot, pred, 1 - pred)
        t = F.ones_like(one_hot)
        alpha = F.where(one_hot, self._alpha * t, (1 - self._alpha) * t)
        loss = -alpha * ((1 - pt) ** self._gamma) * F.log(F.minimum(pt + self._eps, 1))
        loss = _apply_weighting(F, loss, self._weight, sample_weight)
        if self._size_average:
            return F.mean(loss, axis=self._batch_axis, exclude=True)
        else:
            return F.sum(loss, axis=self._batch_axis, exclude=True)

def _as_list(arr):
    """Make sure input is a list of mxnet NDArray"""
    if not isinstance(arr, (list, tuple)):
        return [arr]
    return arr


class SSDMultiBoxLoss(gluon.Block):
    r"""Single-Shot Multibox Object Detection Loss.

    .. note::

        Since cross device synchronization is required to compute batch-wise statistics,
        it is slightly sub-optimal compared with non-sync version. However, we find this
        is better for converged model performance.

    Parameters
    ----------
    negative_mining_ratio : float, default is 3
        Ratio of negative vs. positive samples.
    rho : float, default is 1.0
        Threshold for trimmed mean estimator. This is the smooth parameter for the
        L1-L2 transition.
    lambd : float, default is 1.0
        Relative weight between classification and box regression loss.
        The overall loss is computed as :math:`L = loss_{class} + \lambda \times loss_{loc}`.
    min_hard_negatives : int, default is 0
        Minimum number of negatives samples.

    """
    def __init__(self, negative_mining_ratio=3, rho=1.0, lambd=1.0,
                 min_hard_negatives=0, **kwargs):
        super(SSDMultiBoxLoss, self).__init__(**kwargs)
        self._negative_mining_ratio = max(0, negative_mining_ratio)
        self._rho = rho
        self._lambd = lambd
        self._min_hard_negatives = max(0, min_hard_negatives)

    def forward(self, cls_pred, box_pred, cls_target, box_target):
        """Compute loss in entire batch across devices."""
        # require results across different devices at this time
        cls_pred, box_pred, cls_target, box_target = [_as_list(x) \
            for x in (cls_pred, box_pred, cls_target, box_target)]
        # cross device reduction to obtain positive samples in entire batch
        num_pos = []
        for cp, bp, ct, bt in zip(*[cls_pred, box_pred, cls_target, box_target]):
            pos_samples = (ct > 0)
            num_pos.append(pos_samples.sum())
        num_pos_all = sum([p.asscalar() for p in num_pos])
        if num_pos_all < 1 and self._min_hard_negatives < 1:
            # no positive samples and no hard negatives, return dummy losses
            cls_losses = [nd.sum(cp * 0) for cp in cls_pred]
            box_losses = [nd.sum(bp * 0) for bp in box_pred]
            sum_losses = [nd.sum(cp * 0) + nd.sum(bp * 0) for cp, bp in zip(cls_pred, box_pred)]
            return sum_losses, cls_losses, box_losses


        # compute element-wise cross entropy loss and sort, then perform negative mining
        cls_losses = []
        box_losses = []
        sum_losses = []
        for cp, bp, ct, bt in zip(*[cls_pred, box_pred, cls_target, box_target]):
            pred = nd.log_softmax(cp, axis=-1)
            pos = ct > 0
            cls_loss = -nd.pick(pred, ct, axis=-1, keepdims=False)
            rank = (cls_loss * (pos - 1)).argsort(axis=1).argsort(axis=1)
            hard_negative = rank < nd.maximum(self._min_hard_negatives, pos.sum(axis=1)
                                              * self._negative_mining_ratio).expand_dims(-1)
            # mask out if not positive or negative
            cls_loss = nd.where((pos + hard_negative) > 0, cls_loss, nd.zeros_like(cls_loss))
            cls_losses.append(nd.sum(cls_loss, axis=0, exclude=True) / max(1., num_pos_all))

            bp = _reshape_like(nd, bp, bt)
            box_loss = nd.abs(bp - bt)
            box_loss = nd.where(box_loss > self._rho, box_loss - 0.5 * self._rho,
                                (0.5 / self._rho) * nd.square(box_loss))
            # box loss only apply to positive samples
            box_loss = box_loss * pos.expand_dims(axis=-1)
            box_losses.append(nd.sum(box_loss, axis=0, exclude=True) / max(1., num_pos_all))
            sum_losses.append(cls_losses[-1] + self._lambd * box_losses[-1])

        return sum_losses, cls_losses, box_losses


class YOLOV3Loss(Loss):
    """Losses of YOLO v3.

    Parameters
    ----------
    batch_axis : int, default 0
        The axis that represents mini-batch.
    weight : float or None
        Global scalar weight for loss.

    """
    def __init__(self, batch_axis=0, weight=None, **kwargs):
        super(YOLOV3Loss, self).__init__(weight, batch_axis, **kwargs)
        self._sigmoid_ce = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=False)
        self._l1_loss = gluon.loss.L1Loss()

    def hybrid_forward(self, F, objness, box_centers, box_scales, cls_preds,
                       objness_t, center_t, scale_t, weight_t, class_t, class_mask):
        """Compute YOLOv3 losses.

        Parameters
        ----------
        objness : mxnet.nd.NDArray
            Predicted objectness (B, N), range (0, 1).
        box_centers : mxnet.nd.NDArray
            Predicted box centers (x, y) (B, N, 2), range (0, 1).
        box_scales : mxnet.nd.NDArray
            Predicted box scales (width, height) (B, N, 2).
        cls_preds : mxnet.nd.NDArray
            Predicted class predictions (B, N, num_class), range (0, 1).
        objness_t : mxnet.nd.NDArray
            Objectness target, (B, N), 0 for negative 1 for positive, -1 for ignore.
        center_t : mxnet.nd.NDArray
            Center (x, y) targets (B, N, 2).
        scale_t : mxnet.nd.NDArray
            Scale (width, height) targets (B, N, 2).
        weight_t : mxnet.nd.NDArray
            Loss Multipliers for center and scale targets (B, N, 2).
        class_t : mxnet.nd.NDArray
            Class targets (B, N, num_class).
            It's relaxed one-hot vector, i.e., (1, 0, 1, 0, 0).
            It can contain more than one positive class.
        class_mask : mxnet.nd.NDArray
            0 or 1 mask array to mask out ignored samples (B, N, num_class).

        Returns
        -------
        tuple of NDArrays
            obj_loss: sum of objectness logistic loss
            center_loss: sum of box center logistic regression loss
            scale_loss: sum of box scale l1 loss
            cls_loss: sum of per class logistic loss

        """
        # compute some normalization count, except batch-size
        denorm = F.cast(
            F.shape_array(objness_t).slice_axis(axis=0, begin=1, end=None).prod(), 'float32')
        weight_t = F.broadcast_mul(weight_t, objness_t)
        hard_objness_t = F.where(objness_t > 0, F.ones_like(objness_t), objness_t)
        new_objness_mask = F.where(objness_t > 0, objness_t, objness_t >= 0)
        obj_loss = F.broadcast_mul(
            self._sigmoid_ce(objness, hard_objness_t, new_objness_mask), denorm)
        center_loss = F.broadcast_mul(self._sigmoid_ce(box_centers, center_t, weight_t), denorm * 2)
        scale_loss = F.broadcast_mul(self._l1_loss(box_scales, scale_t, weight_t), denorm * 2)
        denorm_class = F.cast(
            F.shape_array(class_t).slice_axis(axis=0, begin=1, end=None).prod(), 'float32')
        class_mask = F.broadcast_mul(class_mask, objness_t)
        cls_loss = F.broadcast_mul(self._sigmoid_ce(cls_preds, class_t, class_mask), denorm_class)
        return obj_loss, center_loss, scale_loss, cls_loss


class SoftmaxCrossEntropyLoss(Loss):
    r"""SoftmaxCrossEntropyLoss with ignore labels

    Parameters
    ----------
    axis : int, default -1
        The axis to sum over when computing softmax and entropy.
    sparse_label : bool, default True
        Whether label is an integer array instead of probability distribution.
    from_logits : bool, default False
        Whether input is a log probability (usually from log_softmax) instead
        of unnormalized numbers.
    weight : float or None
        Global scalar weight for loss.
    batch_axis : int, default 0
        The axis that represents mini-batch.
    ignore_label : int, default -1
        The label to ignore.
    size_average : bool, default False
        Whether to re-scale loss with regard to ignored labels.
    """
    def __init__(self, sparse_label=True, batch_axis=0, ignore_label=-1,
                 size_average=True, **kwargs):
        super(SoftmaxCrossEntropyLoss, self).__init__(None, batch_axis, **kwargs)
        self._sparse_label = sparse_label
        self._ignore_label = ignore_label
        self._size_average = size_average

    def hybrid_forward(self, F, pred, label):
        """Compute loss"""
        softmaxout = F.SoftmaxOutput(
            pred, label.astype(pred.dtype), ignore_label=self._ignore_label,
            multi_output=self._sparse_label,
            use_ignore=True, normalization='valid' if self._size_average else 'null')
        if self._sparse_label:
            loss = -F.pick(F.log(softmaxout), label, axis=1, keepdims=True)
        else:
            label = _reshape_like(F, label, pred)
            loss = -F.sum(F.log(softmaxout) * label, axis=-1, keepdims=True)
        loss = F.where(label.expand_dims(axis=1) == self._ignore_label,
                       F.zeros_like(loss), loss)
        return F.mean(loss, axis=self._batch_axis, exclude=True)

class MixSoftmaxCrossEntropyLoss(SoftmaxCrossEntropyLoss):
    """SoftmaxCrossEntropyLoss2D with Auxiliary Loss

    Parameters
    ----------
    aux : bool, default True
        Whether to use auxiliary loss.
    aux_weight : float, default 0.2
        The weight for aux loss.
    ignore_label : int, default -1
        The label to ignore.
    """
    def __init__(self, aux=True, mixup=False, aux_weight=0.2, ignore_label=-1, **kwargs):
        super(MixSoftmaxCrossEntropyLoss, self).__init__(
            ignore_label=ignore_label, **kwargs)
        self.aux = aux
        self.mixup = mixup
        self.aux_weight = aux_weight

    def _aux_forward(self, F, pred1, pred2, label, **kwargs):
        """Compute loss including auxiliary output"""
        loss1 = super(MixSoftmaxCrossEntropyLoss, self). \
            hybrid_forward(F, pred1, label, **kwargs)
        loss2 = super(MixSoftmaxCrossEntropyLoss, self). \
            hybrid_forward(F, pred2, label, **kwargs)
        return loss1 + self.aux_weight * loss2

    def _aux_mixup_forward(self, F, pred1, pred2, label1, label2, lam):
        """Compute loss including auxiliary output"""
        loss1 = self._mixup_forward(F, pred1, label1, label2, lam)
        loss2 = self._mixup_forward(F, pred2, label1, label2, lam)
        return loss1 + self.aux_weight * loss2

    def _mixup_forward(self, F, pred, label1, label2, lam, sample_weight=None):
        if not self._from_logits:
            pred = F.log_softmax(pred, self._axis)
        if self._sparse_label:
            loss1 = -F.pick(pred, label1, axis=self._axis, keepdims=True)
            loss2 = -F.pick(pred, label2, axis=self._axis, keepdims=True)
            loss = lam * loss1 + (1 - lam) * loss2
        else:
            label1 = _reshape_like(F, label1, pred)
            label2 = _reshape_like(F, label2, pred)
            loss1 = -F.sum(pred*label1, axis=self._axis, keepdims=True)
            loss2 = -F.sum(pred*label2, axis=self._axis, keepdims=True)
            loss = lam * loss1 + (1 - lam) * loss2
        loss = _apply_weighting(F, loss, self._weight, sample_weight)
        return F.mean(loss, axis=self._batch_axis, exclude=True)

    def hybrid_forward(self, F, *inputs, **kwargs):
        """Compute loss"""
        if self.aux:
            if self.mixup:
                return self._aux_mixup_forward(F, *inputs, **kwargs)
            else:
                return self._aux_forward(F, *inputs, **kwargs)
        else:
            if self.mixup:
                return self._mixup_forward(F, *inputs, **kwargs)
            else:
                return super(MixSoftmaxCrossEntropyLoss, self). \
                    hybrid_forward(F, *inputs, **kwargs)

class SoftmaxCrossEntropyOHEMLoss(Loss):
    r"""SoftmaxCrossEntropyLoss with ignore labels

    Parameters
    ----------
    axis : int, default -1
        The axis to sum over when computing softmax and entropy.
    sparse_label : bool, default True
        Whether label is an integer array instead of probability distribution.
    from_logits : bool, default False
        Whether input is a log probability (usually from log_softmax) instead
        of unnormalized numbers.
    weight : float or None
        Global scalar weight for loss.
    batch_axis : int, default 0
        The axis that represents mini-batch.
    ignore_label : int, default -1
        The label to ignore.
    size_average : bool, default False
        Whether to re-scale loss with regard to ignored labels.
    """
    def __init__(self, sparse_label=True, batch_axis=0, ignore_label=-1,
                 size_average=True, **kwargs):
        super(SoftmaxCrossEntropyOHEMLoss, self).__init__(None, batch_axis, **kwargs)
        self._sparse_label = sparse_label
        self._ignore_label = ignore_label
        self._size_average = size_average

    def hybrid_forward(self, F, pred, label):
        """Compute loss"""
        softmaxout = F.contrib.SoftmaxOHEMOutput(
            pred, label.astype(pred.dtype), ignore_label=self._ignore_label,
            multi_output=self._sparse_label,
            use_ignore=True, normalization='valid' if self._size_average else 'null',
            thresh=0.6, min_keep=256)
        loss = -F.pick(F.log(softmaxout), label, axis=1, keepdims=True)
        loss = F.where(label.expand_dims(axis=1) == self._ignore_label,
                       F.zeros_like(loss), loss)
        return F.mean(loss, axis=self._batch_axis, exclude=True)

class MixSoftmaxCrossEntropyOHEMLoss(SoftmaxCrossEntropyOHEMLoss):
    """SoftmaxCrossEntropyLoss2D with Auxiliary Loss

    Parameters
    ----------
    aux : bool, default True
        Whether to use auxiliary loss.
    aux_weight : float, default 0.2
        The weight for aux loss.
    ignore_label : int, default -1
        The label to ignore.
    """
    def __init__(self, aux=True, aux_weight=0.2, ignore_label=-1, **kwargs):
        super(MixSoftmaxCrossEntropyOHEMLoss, self).__init__(
            ignore_label=ignore_label, **kwargs)
        self.aux = aux
        self.aux_weight = aux_weight

    def _aux_forward(self, F, pred1, pred2, label, **kwargs):
        """Compute loss including auxiliary output"""
        loss1 = super(MixSoftmaxCrossEntropyOHEMLoss, self). \
            hybrid_forward(F, pred1, label, **kwargs)
        loss2 = super(MixSoftmaxCrossEntropyOHEMLoss, self). \
            hybrid_forward(F, pred2, label, **kwargs)
        return loss1 + self.aux_weight * loss2

    def hybrid_forward(self, F, *inputs, **kwargs):
        """Compute loss"""
        if self.aux:
            return self._aux_forward(F, *inputs, **kwargs)
        else:
            return super(MixSoftmaxCrossEntropyOHEMLoss, self). \
                hybrid_forward(F, *inputs, **kwargs)

class DistillationSoftmaxCrossEntropyLoss(gluon.HybridBlock):
    """SoftmaxCrossEntrolyLoss with Teacher model prediction

    Parameters
    ----------
    temperature : float, default 1
        The temperature parameter to soften teacher prediction.
    hard_weight : float, default 0.5
        The weight for loss on the one-hot label.
    sparse_label : bool, default True
        Whether the one-hot label is sparse.
    """
    def __init__(self, temperature=1, hard_weight=0.5, sparse_label=True, **kwargs):
        super(DistillationSoftmaxCrossEntropyLoss, self).__init__(**kwargs)
        self._temperature = temperature
        self._hard_weight = hard_weight
        with self.name_scope():
            self.soft_loss = gluon.loss.SoftmaxCrossEntropyLoss(sparse_label=False, **kwargs)
            self.hard_loss = gluon.loss.SoftmaxCrossEntropyLoss(sparse_label=sparse_label, **kwargs)

    def hybrid_forward(self, F, output, label, soft_target):
        # pylint: disable=unused-argument
        """Compute loss"""
        if self._hard_weight == 0:
            return (self._temperature ** 2) * self.soft_loss(output / self._temperature,
                                                             soft_target)
        elif self._hard_weight == 1:
            return self.hard_loss(output, label)
        else:
            soft_loss = (self._temperature ** 2) * self.soft_loss(output / self._temperature,
                                                                  soft_target)
            hard_loss = self.hard_loss(output, label)
            return (1 - self._hard_weight) * soft_loss  + self._hard_weight * hard_loss


class YOLACTMultiBoxLoss(gluon.Block):
    def __init__(self, negative_mining_ratio=3, rho=1.0, box_lambd=1.5, conf_lambd=1.0, mask_lambd=1.25,
                 min_hard_negatives=0, **kwargs):
        super(YOLACTMultiBoxLoss, self).__init__(**kwargs)
        self._negative_mining_ratio = max(0, negative_mining_ratio)
        self._rho = rho
        self.box_lambd = box_lambd
        self.conf_lambd = conf_lambd
        self.mask_lambd = mask_lambd
        self.gt_weidth = 550
        self.gt_height = 550
        self._min_hard_negatives = max(0, min_hard_negatives)
        self.SBCELoss = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=True)

    def crop(self, bboxes, h, w, masks):
        scale = 4
        b = bboxes.shape[0]
        ctx = bboxes.context
        with autograd.pause():
            _h = nd.arange(h, ctx = ctx)
            _w = nd.arange(w, ctx = ctx)
            _h = nd.tile(_h, reps=(b, 1))
            _w = nd.tile(_w, reps=(b, 1))
            x1, y1 = nd.round(bboxes[:, 0]/scale), nd.round(bboxes[:, 1]/scale)
            x2, y2 = nd.round((bboxes[:, 2])/scale), nd.round((bboxes[:, 3])/scale)
            _w = (_w >= x1.expand_dims(axis=-1)) * (_w <= x2.expand_dims(axis=-1))
            _h = (_h >= y1.expand_dims(axis=-1)) * (_h <= y2.expand_dims(axis=-1))
            _mask = nd.batch_dot(_h.expand_dims(axis=-1),  _w.expand_dims(axis=-1), transpose_b=True)
        masks = _mask * masks
        return masks

    def global_aware(self, masks):
        _, h, w = masks.shape
        masks = masks.reshape((0, -1))
        masks = masks - nd.mean(masks, axis=-1, keepdims=True)
        std = nd.sqrt(nd.mean(nd.square(masks), axis=-1, keepdims=True))
        masks = (masks / (std + 1e-6)).reshape((0, h, w))
        return masks

    def mask_loss(self, mask_pred, mask_eoc, mask_target, matches, bt_target):
        samples = matches >= 0
        pos_num = samples.sum(axis=-1).asnumpy().astype('int')
        rank = (-matches).argsort(axis=-1)
        # pos_bboxes = []
        # pos_masks = []
        # mask_preds = []
        losses = []
        for i in range(mask_pred.shape[0]):
            if pos_num[i] == 0:
                losses.append(nd.zeros(shape=(1,), ctx=mask_pred.context))
                continue
            idx = rank[i, :pos_num[i]]
            pos_bboxe = nd.take(bt_target[i], idx)
            area = (pos_bboxe[:, 3] - pos_bboxe[:, 1]) * (pos_bboxe[:, 2] - pos_bboxe[:, 0])
            weight = self.gt_weidth * self.gt_height / area
            mask_gt = mask_target[i, matches[i, idx], :, :]
            mask_preds = nd.sigmoid(nd.dot(nd.take(mask_eoc[i], idx), mask_pred[i]))
            _, h, w = mask_preds.shape
            mask_preds = self.crop(pos_bboxe, h, w, mask_preds)
            loss = self.SBCELoss(mask_preds, mask_gt) * weight
            # loss = 0.5 * nd.square(mask_gt - mask_preds) / (mask_gt.shape[0]*mask_gt.shape[1]*mask_gt.shape[2])
            losses.append(nd.mean(loss))
        return nd.concat(*losses, dim=0)

    def forward(self, cls_pred, box_pred, mask_pred, mask_eoc, cls_target, box_target, mask_target, matches, bts):
        """Compute loss in entire batch across devices."""
        # require results across different devices at this time
        cls_pred, box_pred, cls_target, box_target = [_as_list(x) \
            for x in (cls_pred, box_pred, cls_target, box_target)]
        # cross device reduction to obtain positive samples in entire batch
        num_pos = []
        for cp, bp, ct, bt in zip(*[cls_pred, box_pred, cls_target, box_target]):
            pos_samples = (ct > 0)
            num_pos.append(pos_samples.sum())
        num_pos_all = sum([p.asscalar() for p in num_pos])
        if num_pos_all < 1 and self._min_hard_negatives < 1:
            # no positive samples and no hard negatives, return dummy losses
            cls_losses = [nd.sum(cp * 0) for cp in cls_pred]
            box_losses = [nd.sum(bp * 0) for bp in box_pred]
            mask_losses = [nd.sum(me * 0) for me in mask_eoc]
            sum_losses = [nd.sum(cp * 0) + nd.sum(bp * 0) + nd.sum(me * 0) for cp, bp, me in zip(cls_pred, box_pred, mask_eoc)]
            return sum_losses, cls_losses, box_losses, mask_losses


        # compute element-wise cross entropy loss and sort, then perform negative mining
        cls_losses = []
        box_losses = []
        sum_losses = []
        mask_losses = []
        for cp, bp, ct, bt, mp, me, mt, ma, btt in zip(*[cls_pred, box_pred, cls_target, box_target, mask_pred, mask_eoc, mask_target, matches, bts]):
            # mask loss
            mask_losses.append(self.mask_lambd * self.mask_loss(mp, me, mt, ma, btt))

            pred = nd.log_softmax(cp, axis=-1)
            pos = ct > 0
            cls_loss = -nd.pick(pred, ct, axis=-1, keepdims=False)
            rank = (cls_loss * (pos - 1)).argsort(axis=1).argsort(axis=1)
            hard_negative = rank < nd.maximum(self._min_hard_negatives, pos.sum(axis=1)
                                              * self._negative_mining_ratio).expand_dims(-1)
            # mask out if not positive or negative
            cls_loss = nd.where((pos + hard_negative) > 0, cls_loss, nd.zeros_like(cls_loss))
            cls_losses.append(self.conf_lambd * nd.sum(cls_loss, axis=0, exclude=True) / max(1., num_pos_all))

            bp = _reshape_like(nd, bp, bt)
            box_loss = nd.abs(bp - bt)
            box_loss = nd.where(box_loss > self._rho, box_loss - 0.5 * self._rho,
                                (0.5 / self._rho) * nd.square(box_loss))
            # box loss only apply to positive samples
            box_loss = box_loss * pos.expand_dims(axis=-1)
            box_losses.append(self.box_lambd * nd.sum(box_loss, axis=0, exclude=True) / max(1., num_pos_all))
            sum_losses.append(cls_losses[-1] + box_losses[-1] + mask_losses[-1])

        return sum_losses, cls_losses, box_losses, mask_losses

class IOULoss(Loss):
    """Implementation of IOULoss Per Card."""
    def __init__(self, return_iou, eps=1e-5, weight=None, batch_axis=0, **kwargs):
        super(IOULoss, self).__init__(weight, batch_axis, **kwargs)
        self._return_iou = return_iou
        self._eps = eps

    def hybrid_forward(self, F, pred, gt, label):
        """
        pred : [B, N, 4]
        gt : [B, N, 4]
        label : [B, N]
        """
        px1, py1, px2, py2 = F.split(pred, num_outputs=4, axis=-1, squeeze_axis=True)
        gx1, gy1, gx2, gy2 = F.split(gt, num_outputs=4, axis=-1, squeeze_axis=True)
        apd = F.abs(px2 - px1 + 1) * F.abs(py2 - py1 + 1)
        agt = F.abs(gx2 - gx1 + 1) * F.abs(gy2 - gy1 + 1)

        iw = F.maximum(F.minimum(px2, gx2) - F.maximum(px1, gx1)+1., 0.)
        ih = F.maximum(F.minimum(py2, gy2) - F.maximum(py1, gy1)+1., 0.)
        ain = iw * ih + 1.
        union = apd + agt - ain + 1.
        ious = F.maximum(ain / union, 0.)
        # label = F.squeeze(label, axis=-1)
        fg_mask = F.where(label > 0, F.ones_like(label), F.zeros_like(label))
        loss = -F.log(F.minimum(ious + self._eps, 1.)) * fg_mask
        if self._return_iou:
            return F.sum(loss) / F.maximum(F.sum(fg_mask), 1), ious
        return F.sum(loss) / F.maximum(F.sum(fg_mask), 1)

class CtrNessLoss(Loss):
    """Implementation of CenterNess Loss Per Card."""
    def __init__(self, eps=1e-5,  weight=None, batch_axis=0, **kwargs):
        super(CtrNessLoss, self).__init__(weight, batch_axis, **kwargs)

    def hybrid_forward(self, F, pred, ctr_gt, cls_gt):
        pred = F.squeeze(pred, axis=-1)
        pos_gt_mask = cls_gt > 0
        pos_pred_mask = pred >= 0
        loss = (pred * pos_pred_mask - pred * ctr_gt + F.log(1 + \
                F.exp(-F.abs(pred)))) * pos_gt_mask
        return F.sum(loss) / F.maximum(F.sum(pos_gt_mask), 1)

class SigmoidFocalLoss(Loss):
    """SigmoidFocalLoss Per GPU Card."""
    def __init__(self, alpha=0.25, gamma=2, sparse_label=True, from_logits=False,
                 batch_axis=0, weight=None, num_class=None, eps=1e-12, **kwargs):
        super(SigmoidFocalLoss, self).__init__(weight, batch_axis, **kwargs)
        self._alpha = alpha
        self._gamma = gamma
        self._sparse_label = sparse_label
        if sparse_label and (not isinstance(num_class, int) or (num_class < 1)):
            raise ValueError("Number of class > 0 must be provided if sparse label is used.")
        self._num_class = num_class
        self._from_logits = from_logits
        self._eps = eps

    def hybrid_forward(self, F, pred, label, sample_weight=None):
        """Loss forward"""
        if not self._from_logits:
            pred = F.sigmoid(pred)
        one_hot = F.one_hot(label, self._num_class)
        one_hot = F.slice_axis(one_hot, begin=1, end=None, axis=-1)
        pt = F.where(one_hot, pred, 1 - pred)
        t = F.ones_like(one_hot)
        alpha = F.where(one_hot, self._alpha * t, (1 - self._alpha) * t)
        loss = -alpha * ((1 - pt) ** self._gamma) * F.log(F.minimum(pt + self._eps, 1))
        loss = _apply_weighting(F, loss, self._weight, sample_weight)

        # Method 2:
        # pos_part = F.power(1 - pred, self._gamma) * one_hot * \
        #         F.log(pred + self._eps)
        # neg_part = F.power(pred, self._gamma) * (1 - one_hot) * \
        #         F.log(1 - pred + self._eps)
        # loss = -F.sum(self._alpha * pos_part + (1 - self._alpha) * neg_part, axis=-1)
        # loss = _apply_weighting(F, loss, self._weight, sample_weight)
        pos_mask = (label > 0)
        return F.sum(loss) / F.maximum(F.sum(pos_mask), 1)

class MaskLoss(gluon.Block):
    def __init__(self, mask_lambd=1.25, **kwargs):
        super(MaskLoss, self).__init__(**kwargs)
        self.gt_weidth = 740
        self.gt_height = 740
        self.mask_lambd = mask_lambd
        self.SBCELoss = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=True)

    def crop(self, bboxes, h, w, masks):
        scale = 4
        b = bboxes.shape[0]
        ctx = bboxes.context
        with autograd.pause():
            _h = nd.arange(h, ctx = ctx)
            _w = nd.arange(w, ctx = ctx)
            _h = nd.tile(_h, reps=(b, 1))
            _w = nd.tile(_w, reps=(b, 1))
            x1, y1 = nd.round(bboxes[:, 0]/scale), nd.round(bboxes[:, 1]/scale)
            x2, y2 = nd.round((bboxes[:, 2])/scale), nd.round((bboxes[:, 3])/scale)
            _w = (_w >= x1.expand_dims(axis=-1)) * (_w <= x2.expand_dims(axis=-1))
            _h = (_h >= y1.expand_dims(axis=-1)) * (_h <= y2.expand_dims(axis=-1))
            _mask = nd.batch_dot(_h.expand_dims(axis=-1),  _w.expand_dims(axis=-1), transpose_b=True)
        masks = _mask * masks
        return masks

    def global_aware(self, masks):
        _, h, w = masks.shape
        masks = masks.reshape((0, -1))
        masks = masks - nd.mean(masks, axis=-1, keepdims=True)
        std = nd.sqrt(nd.mean(nd.square(masks), axis=-1, keepdims=True))
        masks = (masks / (std + 1e-6)).reshape((0, h, w))
        return masks

    def mask_loss(self, mask_pred, mask_eoc, mask_target, matches, bt_target):
        samples = (matches >= 0)
        pos_num = samples.sum(axis=-1).asnumpy().astype('int')
        rank = (-matches).argsort(axis=-1)
        losses = []
        for i in range(mask_pred.shape[0]):
            if pos_num[i] == 0:
                losses.append(nd.zeros(shape=(1,), ctx=mask_pred.context))
                continue
            idx = rank[i, :pos_num[i]]
            pos_bboxe = nd.take(bt_target[i], idx)
            area = (pos_bboxe[:, 3] - pos_bboxe[:, 1]) * (pos_bboxe[:, 2] - pos_bboxe[:, 0])
            weight = self.gt_weidth * self.gt_height / area
            mask_gt = mask_target[i, matches[i, idx], :, :]
            mask_preds = nd.dot(nd.take(mask_eoc[i], idx), mask_pred[i])
            _, h, w = mask_preds.shape
            # mask_preds = self.global_aware(mask_preds)
            mask_preds = nd.sigmoid(mask_preds)
            mask_preds = self.crop(pos_bboxe, h, w, mask_preds)
            loss = self.SBCELoss(mask_preds, mask_gt) * weight
            losses.append(nd.mean(loss))
        return nd.concat(*losses, dim=0)

    def forward(self, box_target, gt_masks, matches, masks, maskeoc_pred):
        mask_loss = self.mask_lambd * self.mask_loss(masks, maskeoc_pred, gt_masks, matches, box_target)
        return nd.mean(mask_loss)

class MaskFCOSLoss(gluon.Block):
    def __init__(self, return_iou, alpha=0.25, gamma=2, cls_lambd=1., box_lambd=1., ctr_lambd=1.,
                 mask_lambd=6.125, img_shape=(740, 740), sparse_label=True, from_logits=False,
                 num_class=None, eps=1e-5, **kwargs):
        super(MaskFCOSLoss, self).__init__(**kwargs)
        self._return_iou = return_iou
        self._eps = eps
        self._alpha = alpha
        self._gamma = gamma
        self._cls_lambd = cls_lambd
        self._box_lambd = box_lambd
        self._ctr_lambd = ctr_lambd
        self._mask_lambd = mask_lambd
        self._sparse_label = sparse_label
        if sparse_label and (not isinstance(num_class, int) or (num_class < 1)):
            raise ValueError("Nomber of class > 0 must be provided if sparse label is used.")
        self._num_class = num_class
        self._from_logits = from_logits
        self.gt_weidth, self.gt_height = img_shape
        self.SBCELoss = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=True)
        self.max=0

    def forward(self, cls_targets, ctr_targets, box_targets, mask_targets, matches,
                cls_preds, ctr_preds, box_preds, mask_preds, maskcoe_preds):
        """Compute loss in entire batch across devices."""
        scale = 4
        # require results across different devices at this time
        cls_targets, ctr_targets, box_targets, mask_targets, matches, cls_preds, ctr_preds, box_preds, mask_preds, maskcoe_preds = \
            [_as_list(x) for x in (cls_targets, ctr_targets, box_targets, mask_targets, matches,
                                   cls_preds, ctr_preds, box_preds, mask_preds, maskcoe_preds)]
        # compute element-wise cross entropy loss and sort, then perform negative mining
        cls_losses = []
        ctr_losses = []
        box_losses = []
        mask_losses = []
        sum_losses = []
        for clst, ctrt, boxt, maskt, matche, clsp, ctrp, boxp, maskp, maskcoep in zip(
                *[cls_targets, ctr_targets, box_targets, mask_targets, matches,
                  cls_preds, ctr_preds, box_preds, mask_preds, maskcoe_preds]):

            pos_gt_mask = clst > 0
            # cls loss
            if not self._from_logits:
                clsp = nd.sigmoid(clsp)
            one_hot = nd.one_hot(clst, self._num_class)
            one_hot = nd.slice_axis(one_hot, begin=1, end=None, axis=-1)
            pt = nd.where(one_hot, clsp, 1 - clsp)
            t = nd.ones_like(one_hot)
            alpha = nd.where(one_hot, self._alpha * t, (1 - self._alpha) * t)
            cls_loss = -alpha * ((1 - pt) ** self._gamma) * nd.log(nd.minimum(pt + self._eps, 1))
            cls_loss = nd.sum(cls_loss) / nd.maximum(nd.sum(pos_gt_mask), 1)
            cls_losses.append(cls_loss)

            # ctr loss
            ctrp = nd.squeeze(ctrp, axis=-1)
            pos_pred_mask = ctrp >= 0
            ctr_loss = (ctrp * pos_pred_mask - ctrp * ctrt + nd.log(1 + nd.exp(-nd.abs(ctrp)))) * pos_gt_mask
            ctr_loss = nd.sum(ctr_loss) / nd.maximum(nd.sum(pos_gt_mask), 1)
            ctr_losses.append(ctr_loss)

            # box loss // iou loss
            px1, py1, px2, py2 = nd.split(boxp, num_outputs=4, axis=-1, squeeze_axis=True)
            gx1, gy1, gx2, gy2 = nd.split(boxt, num_outputs=4, axis=-1, squeeze_axis=True)
            apd = nd.abs(px2 - px1 + 1) * nd.abs(py2 - py1 + 1)
            agt = nd.abs(gx2 - gx1 + 1) * nd.abs(gy2 - gy1 + 1)
            iw = nd.maximum(nd.minimum(px2, gx2) - nd.maximum(px1, gx1) + 1., 0.)
            ih = nd.maximum(nd.minimum(py2, gy2) - nd.maximum(py1, gy1) + 1., 0.)
            ain = iw * ih + 1.
            union = apd + agt - ain + 1
            ious = nd.maximum(ain / union, 0.)
            fg_mask = nd.where(clst > 0, nd.ones_like(clst), nd.zeros_like(clst))
            box_loss = -nd.log(nd.minimum(ious + self._eps, 1.)) * fg_mask
            if self._return_iou:
                box_loss = nd.sum(box_loss) / nd.maximum(nd.sum(fg_mask), 1), ious
            else:
                box_loss = nd.sum(box_loss) / nd.maximum(nd.sum(fg_mask), 1)
            box_losses.append(box_loss)

            # mask loss
            rank = (-matche).argsort(axis=-1)
            rank = nd.split(rank, 2, axis=0, squeeze_axis=True)
            matche = nd.split(matche, 2, axis=0, squeeze_axis=True)
            maskp = nd.split(maskp, 2, axis=0, squeeze_axis=True)
            maskt = nd.split(maskt, 2, axis=0, squeeze_axis=True)
            boxt = nd.split(boxt, 2, axis=0, squeeze_axis=True)
            maskcoep = nd.split(maskcoep, 2, axis=0, squeeze_axis=True)
            agt = nd.split(agt, 2, axis=0, squeeze_axis=True)
            mask_loss = []
            for ranki, matchei, maskpi, maskti, boxti, maskcoepi, agti in zip(rank, matche, maskp,
                                                                                       maskt, boxt, maskcoep, agt):
                idx = nd.slice(ranki, 0, 200)
                pos_mask = nd.take(matchei >= 0, idx)
                pos_box = nd.take(boxti, idx)
                area = nd.take(agti, idx)
                weight = (self.gt_weidth * self.gt_height / (area+self._eps)) * pos_mask
                mask_idx = nd.take(matchei, idx)
                maskti = nd.take(maskti, mask_idx)
                maskpi = nd.dot(nd.take(maskcoepi, idx), maskpi)
                maskpi = nd.sigmoid(maskpi)
                with autograd.pause():
                    _h = nd.arange(186, ctx=maskpi.context)
                    _w = nd.arange(186, ctx=maskpi.context)
                    _h = nd.tile(_h, reps=(pos_box.shape[0], 1))
                    _w = nd.tile(_w, reps=(pos_box.shape[0], 1))
                    x1, y1, x2, y2 = nd.split(nd.round(pos_box / scale), num_outputs=4, axis=-1)
                    _w = (_w >= x1) * (_w <= x2)
                    _h = (_h >= y1) * (_h <= y2)
                    _mask = nd.batch_dot(_h.expand_dims(axis=-1), _w.expand_dims(axis=-1), transpose_b=True)
                maskpi = maskpi * _mask
                mask_loss.append(nd.sum(self.SBCELoss(maskpi, maskti) * weight) / nd.sum(pos_mask + self._eps))


            # if sum(pos_num)>1400:
            #     print(sum(pos_num))
            #     print(pos_num)
            # pos_num = (matche >=0).sum(axis=-1).asnumpy()
            # rank = (-matche).argsort(axis=-1)
            # mask_loss = []
            # for i in range(maskp.shape[0]):
            #     if pos_num[i] == 0.:
            #         # print(pos_num)
            #         mask_loss.append(nd.zeros(shape=(1,), ctx=maskp.context))
            #         continue
            #     idx = rank[i, :int(pos_num[i])]
            #     pos_box = nd.take(boxt[i], idx)
            #     area = (pos_box[:, 3] - pos_box[:, 1]) * (pos_box[:, 2] - pos_box[:, 0])
            #     weight = self.gt_weidth * self.gt_height / (area+self._eps)
            #     maskti = maskt[i, matche[i, idx], :, :]
            #     maskpi = nd.dot(nd.take(maskcoep[i], idx), maskp[i])
            #     _, h, w = maskpi.shape
            #     maskpi = nd.sigmoid(maskpi)
            #     with autograd.pause():
            #         _h = nd.arange(h, ctx=maskpi.context)
            #         _w = nd.arange(w, ctx=maskpi.context)
            #         _h = nd.tile(_h, reps=(pos_box.shape[0], 1))
            #         _w = nd.tile(_w, reps=(pos_box.shape[0], 1))
            #         x1, y1, x2, y2 = nd.split(nd.round(pos_box / scale), num_outputs=4, axis=-1)
            #         _w = (_w >= x1) * (_w <= x2)
            #         _h = (_h >= y1) * (_h <= y2)
            #         _mask = nd.batch_dot(_h.expand_dims(axis=-1), _w.expand_dims(axis=-1), transpose_b=True)
            #     maskpi = maskpi * _mask
            #     mask_loss.append(nd.sum(self.SBCELoss(maskpi, maskti) * weight)/pos_num[i])
            mask_loss = nd.mean(nd.concat(*mask_loss, dim=0))
            mask_losses.append(mask_loss)
            sum_losses.append(self._cls_lambd * cls_losses[-1] + self._ctr_lambd * ctr_losses[-1] +
                              self._box_lambd * box_losses[-1] + self._mask_lambd * mask_losses[-1])

        return sum_losses, cls_losses, ctr_losses, box_losses, mask_losses