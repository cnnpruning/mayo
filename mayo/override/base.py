import functools
from collections import Sequence, namedtuple

import tensorflow as tf

from mayo.log import log
from mayo.util import memoize_property
from mayo.override import util


class OverrideNotAppliedError(Exception):
    """Invoke apply before update.  """


class GetterInvokedOutsideApplyError(Exception):
    """Function getter() is invoked not in apply()."""


def _getter_not_initialized(*args, **kwargs):
    raise GetterInvokedOutsideApplyError(
        'The function `getter()` should only be invoked in `.apply()`.')


class Parameter(object):
    """ `tf.Variable`-based overrider hyperparameter.  """
    def __init__(self, name, initial, shape=None, dtype=None, trainable=False):
        super().__init__()
        if dtype not in (int, float):
            raise TypeError('Parameter accepts only int or float data-types.')
        self.name = name
        self.initial = initial
        self.shape = shape
        self.dtype = dtype
        self.trainable = trainable

    def __get__(self, instance, owner):
        try:
            return instance._parameter_variables[self.name]
        except KeyError:
            pass
        name = '{}/{}'.format(instance.name, self.name)
        init = tf.constant_initializer(float(self.initial))
        var = instance._getter(
            name, initializer=init, shape=self.shape,
            dtype=tf.float32, trainable=self.trainable)
        instance._parameter_variables[self.name] = var
        if self.dtype is int:
            return util.round(var)
        return var

    def __set__(self, instance, value):
        instance._parameter_variables_assignment[self.name] = value


class OverriderBase(object):
    """
    Base class for applying overriding operations on a Net.  Please ensure
    both methods `_apply` and `_update` are overridden with appropriate
    implementations.

    The method `_apply` overrides the variable in `value`, returns the
    overridden result; `_update` updates states of tensorflow variables used in
    `_apply`.
    """
    _parameter_variables = {}
    _parameter_variables_assignment = {}

    def __init__(self, should_update=True):
        super().__init__()
        self.name = None
        self.internals = {}
        self.should_update = should_update

    @memoize_property
    def parameters(self):
        params = {}
        for key, value in self.__class__.__dict__.items():
            if isinstance(value, Parameter):
                params[key] = value
        return params

    def assign_parameters(self, tf_session):
        ops = []
        for name, value in self._parameter_variables_assignment.items():
            log.debug(
                'Assigning overrider parameter: {}.{} = {}'
                .format(self, name, value))
            ops.append(tf.assign(self._parameter_variables[name], value))
        tf_session.run(ops)
        self._parameter_variables_assignment = {}

    def _apply(self, value):
        """
        Override this method called in `.apply()` to modify the
        variable in `value`.
        """
        raise NotImplementedError(
            'Overrider method `._apply()` must be implemented.')

    def _tracking_getter(self, getter):
        @functools.wraps(getter)
        def wrapped(name, *args, **kwargs):
            var = getter(name, *args, **kwargs)
            self.internals[name] = var
            return var
        return wrapped

    def apply(self, getter, value):
        """
        Things to apply to the variable in `value`, returns the
        overridden result.
        """
        self._applied = True
        self._getter = self._tracking_getter(getter)
        self.name = value.op.name
        self.before = value
        self.after = self._apply(value)
        return self.after

    def _update(self, session):
        """
        Override this method called in `.update()` to update internal
        states of the overrider.
        """
        pass

    def update(self, session):
        """Update things to apply during training.  """
        if not self.should_update:
            return
        if not getattr(self, '_applied', False):
            raise OverrideNotAppliedError(
                'Method "apply" must be invoked before call "update".')
        self._update(session)
        log.debug('Updated overrider {!r}'.format(self.info(session)))

    def assign(self, session):
        """Assign overridden values to parameters before overriding.  """
        session.run(tf.assign(self.before, self.after))

    def reset(self, session):
        """Reset internal variables to their respective initial values.  """
        for var in self.internals.values():
            session.run(tf.assign(var, var.initial_value))

    def _info_tuple(self, **kwargs):
        # relies on dict ordering
        cls = self.__class__.__name__
        cls_name = '{}Info'.format(cls)
        Tuple = namedtuple(cls_name, [cls] + list(kwargs))
        kwargs[cls] = self.name
        return Tuple(**kwargs)

    def info(self, session):
        return self._info_tuple()

    @classmethod
    def finalize_info(cls, table):
        pass

    def __repr__(self):
        if not self.name:
            return super().__repr__()
        return '<{} overrides {!r}>'.format(
            self.__class__.__qualname__, self.name)


class ChainOverrider(OverriderBase, Sequence):
    """ Composition of overriders.  """
    def __init__(self, overriders, should_update=True):
        super().__init__(should_update)
        self._overriders = overriders

    def __getitem__(self, index):
        return self._overriders[index]

    def __len__(self):
        return len(self._overriders)

    def assign_parameters(self, session):
        for o in self._overriders:
            o.assign_parameters(session)

    def _apply(self, value):
        for o in self._overriders:
            value = o.apply(self._getter, value)
        return value

    def _update(self, session):
        for o in self._overriders:
            o.update(session)

    def reset(self, session):
        for o in self._overriders:
            o.reset(session)

    def info(self, session):
        return self._info_tuple(overriders=self._overriders)

    def __repr__(self):
        return repr(self._overriders)