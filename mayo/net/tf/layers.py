import tensorflow as tf
from tensorflow.contrib import slim

from mayo.net.tf.util import use_name_not_scope
from mayo.net.tf.base import TFNetBase


class Layers(TFNetBase):
    """ Create a TensorFlow graph from "config.model" model definition.  """

    def instantiate_convolution(self, node, tensor, params):
        groups = params.pop('num_groups', 1)
        if groups == 1:
            return slim.conv2d(tensor, **params)

        # group-wise convolution
        channels = int(tensor.shape[-1])
        if channels % groups:
            raise ValueError(
                'Number of input channels must be divisible by '
                'the number of groups.')

        # parameters
        scope = params.get('scope')
        normalizer_fn = params.get('normalizer_fn', None)
        activation_fn = params.get('activation_fn', tf.nn.relu)
        params['activation_fn'] = None
        num_outputs = params['num_outputs']
        kernel = params['kernel_size']
        if isinstance(kernel, int):
            kernel = [kernel, kernel]
        stride = params.get('stride', [1, 1])
        if isinstance(stride, int):
            stride = [stride, stride]
        stride = [1] + list(stride) + [1]
        padding = params.get('padding', 'SAME')
        out_channels = groups * num_outputs

        # group weights
        weights_initializer = params.get(
            'weights_initializer', tf.contrib.layers.xavier_initializer())
        weights_regularizer = params.get('weights_regularizer', None)
        weights_shape = kernel + [channels / groups, out_channels]
        weights = tf.get_variable(
            '{}/groupwise_weights'.format(scope), weights_shape,
            initializer=weights_initializer, regularizer=weights_regularizer)
        weights_slices = tf.split(weights, groups, axis=-1)

        # convolution
        input_slices = tf.split(tensor, groups, axis=-1)
        output_slices = []
        iterator = enumerate(zip(input_slices, weights_slices))
        for i, (each, weights) in iterator:
            with tf.name_scope('{}_{}'.format(scope, i)):
                each = tf.nn.conv2d(each, weights, stride, padding)
            output_slices.append(each)
        output = tf.concat(output_slices, axis=-1)

        # add bias
        biases_initializer = params.get(
            'biases_initializer', tf.zeros_initializer())
        biases_regularizer = params.get('biases_regularizer', None)
        biases = tf.get_variable(
            '{}/biases'.format(scope), out_channels,
            initializer=biases_initializer, regularizer=biases_regularizer)
        output = tf.nn.bias_add(output, biases)

        # normalization & activation
        if normalizer_fn:
            output = normalizer_fn(output, scope=scope)
        if activation_fn:
            output = activation_fn(output)
        return output

    def instantiate_depthwise_convolution(self, node, tensor, params):
        multiplier = params.pop('depth_multiplier', 1)
        return slim.separable_conv2d(
            tensor, num_outputs=None, depth_multiplier=multiplier, **params)

    @staticmethod
    def _reduce_kernel_size_for_small_input(params, tensor):
        shape = tensor.get_shape().as_list()
        if shape[1] is None or shape[2] is None:
            return
        kernel = params['kernel_size']
        if isinstance(kernel, int) or kernel is None:
            kernel = [kernel, kernel]
        for i in range(2):
            if kernel[i] is None:
                kernel[i] = shape[i + 1]
        params['kernel_size'] = [
            min(shape[1], kernel[0]), min(shape[2], kernel[1])]

    def _should_pool_nothing(self, params):
        # skip pooling with 1x1 kernel @ stride 1, which is a no-op
        kernel = params['kernel_size'] in (1, [1, 1])
        stride = params.get('stride', 1) == 1
        return kernel and stride

    def instantiate_average_pool(self, node, tensor, params):
        self._reduce_kernel_size_for_small_input(params, tensor)
        if self._should_pool_nothing(params):
            return tensor
        return slim.avg_pool2d(tensor, **params)

    def instantiate_max_pool(self, node, tensor, params):
        self._reduce_kernel_size_for_small_input(params, tensor)
        if self._should_pool_nothing(params):
            return tensor
        return slim.max_pool2d(tensor, **params)

    def instantiate_fully_connected(self, node, tensor, params):
        return slim.fully_connected(tensor, **params)

    def instantiate_softmax(self, node, tensor, params):
        return slim.softmax(tensor, **params)

    def instantiate_dropout(self, node, tensor, params):
        params['is_training'] = self.is_training
        return slim.dropout(tensor, **params)

    def instantiate_local_response_normalization(self, node, tensor, params):
        return tf.nn.local_response_normalization(
            tensor, **use_name_not_scope(params))

    def instantiate_batch_normalization(self, node, tensor, params):
        params['is_training'] = self.is_training
        return slim.batch_norm(tensor, **params)

    def instantiate_squeeze(self, node, tensor, params):
        return tf.squeeze(tensor, **use_name_not_scope(params))

    def instantiate_flatten(self, node, tensor, params):
        return slim.flatten(tensor, **params)

    def instantiate_concat(self, node, tensors, params):
        return tf.concat(tensors, **use_name_not_scope(params))

    def instantiate_add(self, node, tensors, params):
        return tf.add_n(tensors, name=params['scope'])

    def instantiate_mul(self, node, tensors, params):
        if len(tensors) != 2:
            raise ValueError(
                'The function `tf.multiply` expects exactly two inputs.')
        return tf.multiply(tensors[0], tensors[1], name=params['scope'])

    def instantiate_activation(self, node, tensors, params):
        supported_modes = ['relu', 'relu6', 'elu', 'sigmoid', 'tanh']
        mode = params['mode']
        if mode not in supported_modes:
            raise TypeError(
                '{!r} cannot instantiate activation of type {!r}.'
                .format(self, mode))
        func = getattr(tf.nn, mode)
        return func(tensors, name=params['scope'])

    def instantiate_identity(self, node, tensors, params):
        return tensors
