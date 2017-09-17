import re
import os
import ast
import sys
import copy
import glob
import operator
import collections

import yaml


def _unique(items):
    found = set([])
    keep = []
    for item in items:
        if item in found:
            continue
        found.add(item)
        keep.append(item)
    return keep


class YamlTag(object):
    tag = None

    @classmethod
    def register(cls):
        yaml.add_constructor(cls.tag, cls.constructor)
        yaml.add_representer(cls, cls.representer)

    @classmethod
    def constructor(cls, loader, node):
        raise NotImplementedError

    @classmethod
    def representer(cls, dumper, data):
        raise NotImplementedError


class YamlScalarTag(YamlTag):
    def __init__(self, content):
        super().__init__()
        self.content = content

    @classmethod
    def constructor(cls, loader, node):
        content = loader.construct_scalar(node)
        return cls(content)

    @classmethod
    def representer(cls, dumper, tag):
        return dumper.represent_scalar(cls.tag, tag.content)

    def value(self):
        raise NotImplementedError

    def __repr__(self):
        return repr('{} {!r}'.format(self.tag, self.content))


class ArithTag(YamlScalarTag):
    tag = '!arith'
    _eval_expr_map = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.BitXor: operator.xor,
        ast.USub: operator.neg,
    }

    def value(self):
        tree = ast.parse(self.content, mode='eval').body
        return self._eval(tree)

    def _eval(self, n):
        if isinstance(n, ast.Num):
            return n.n
        if isinstance(n, ast.Call):
            op = self._eval(n.func)
            args = (self._eval(a) for a in n.args)
            return op(*args)
        if isinstance(n, ast.Attribute):
            return getattr(self._eval(n.value), n.attr)
        if isinstance(n, ast.Name):
            try:
                return __builtins__[n.id]
            except KeyError:
                return __import__(n.id)
        if not isinstance(n, (ast.UnaryOp, ast.BinOp)):
            raise TypeError('Unrecognized operator node {}'.format(n))
        op = self._eval_expr_map[type(n.op)]
        if isinstance(n, ast.UnaryOp):
            return op(self._eval(n.operand))
        return op(self._eval(n.left), self._eval(n.right))


class ExecTag(YamlScalarTag):
    tag = '!exec'

    def value(self):
        try:
            return self._value
        except AttributeError:
            pass
        variables = {}
        exec(self.content, variables)
        self._value = variables
        return variables


class EvalTag(YamlScalarTag):
    tag = '!eval'

    def value(self):
        try:
            return self._value
        except AttributeError:
            pass
        import tensorflow as tf
        import mayo
        self._value = eval(self.content, {'tf': tf, 'mayo': mayo})
        return self._value


ArithTag.register()
ExecTag.register()


def _dot_path(keyable, dot_path_key):
    *dot_path, final_key = dot_path_key.split('.')
    for key in dot_path:
        try:
            if isinstance(keyable, (tuple, list)):
                value = keyable[int(key)]
            elif isinstance(keyable, dict):
                value = dict.__getitem__(keyable, key)
            else:
                raise TypeError(
                    'Key path {!r} resolution stopped at {!r} because the '
                    'current object {!r} is not key-addressable.'
                    .format(dot_path_key, key, keyable))
        except (KeyError, IndexError):
            raise KeyError('Key path {!r} not found.'.format(dot_path_key))
        if value is None:
            import ipdb
            ipdb.set_trace()
        keyable = value
    return keyable, final_key


class _DotDict(dict):
    def __init__(self, data, root):
        super().__init__(data)
        super().__setattr__('root', root)

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.items():
            result[k] = copy.deepcopy(v, memo)
        return result

    def _recursive_apply(self, obj, func_map):
        if isinstance(obj, dict):
            for k, v in obj.items():
                obj[k] = self._recursive_apply(v, func_map)
        elif isinstance(obj, (tuple, list, set, frozenset)):
            obj = obj.__class__(
                [self._recursive_apply(v, func_map) for v in obj])
        for cls, func in func_map.items():
            if isinstance(obj, cls):
                return func(obj)
        return obj

    def _wrap(self, obj):
        def wrap(obj):
            if type(obj) is dict:
                return _DotDict(obj, self)
            return obj
        return self._recursive_apply(obj, {dict: wrap})

    def _merge(self, d, md):
        for k, md_k in md.items():
            d_k = dict.get(d, k)
            d_map = isinstance(d_k, collections.Mapping)
            md_map = isinstance(md_k, collections.Mapping)
            if d_map and md_map:
                self._merge(d_k, md_k)
            else:
                dict.__setitem__(d, k, md_k)

    def merge(self, md):
        self._merge(self, md)

    def to_dict(self):
        unwrap = lambda obj: dict(obj) if isinstance(obj, dict) else obj
        return self._recursive_apply(copy.deepcopy(self), {dict: unwrap})

    _magic = object()

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            obj, key = _dot_path(self, key)
        except KeyError:
            return False
        if obj is self:
            return super(_DotDict, obj).__contains__(key)
        return key in obj

    def _eval(self, value):
        def eval_tag(value):
            return value.__class__(self._eval(value.content)).value()

        def eval_str(value):
            regex = r'\$\(([_a-zA-Z][_a-zA-Z0-9\.]+)\)'
            keys = re.findall(regex, value, re.MULTILINE)
            for k in keys:
                value = value.replace('$({})'.format(k), str(self.root[k]))
            return value

        eval_map = {YamlScalarTag: eval_tag, str: eval_str}
        return self._recursive_apply(value, eval_map)

    def __getitem__(self, key):
        obj, key = _dot_path(self, key)
        if obj is self:
            value = super(_DotDict, obj).__getitem__(key)
        else:
            value = obj[key]
        return self._eval(value)
    __getattr__ = __getitem__

    def __setitem__(self, key, value):
        obj, key = _dot_path(self, key)
        if obj is self:
            super(_DotDict, obj).__setitem__(key, value)
        else:
            obj[key] = value

    def __delitem__(self, key):
        obj, key = _dot_path(self, key)
        if obj is self:
            super(_DotDict, obj).__delitem__(key)
        else:
            del obj[key]

    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


class Config(_DotDict):
    def __init__(self):
        super().__init__({}, self)
        self._setup_excepthook()
        self._init_system_config()

    def _init_system_config(self):
        root = os.path.dirname(__file__)
        self.yaml_update(os.path.join(root, 'system.yaml'))

    def merge(self, dictionary):
        super().merge(dictionary)
        self._wrap(self)
        if dictionary.get('system', {}).get('log_level'):
            self._setup_log_level()

    def yaml_update(self, file):
        with open(file, 'r') as file:
            self.merge(yaml.load(file))

    def override_update(self, key, value):
        if isinstance(value, str):
            value = yaml.load(value)
        self[key] = value
        if 'system.log_level' in key:
            self._setup_log_level()

    def to_yaml(self, file=None):
        if file is not None:
            file = open(file, 'w')
        kwargs = {'explicit_start': True, 'width': 70, 'indent': 4}
        return yaml.dump(self.to_dict(), file, **kwargs)

    def image_shape(self):
        params = self.dataset.preprocess.shape
        return (params.height, params.width, params.channels)

    def label_offset(self):
        bg = self.dataset.background_class
        return int(bg.use) - int(bg.has)

    def num_classes(self):
        return self.dataset.num_classes + self.label_offset()

    def data_files(self, mode):
        path = self.dataset.path
        try:
            path = path[mode]
        except KeyError:
            raise KeyError('Mode {!r} not recognized.'.format(mode))
        files = []
        search_paths = self.system.search_paths.datasets
        paths = [path]
        if not os.path.isabs(path):
            paths = [os.path.join(d, path) for d in search_paths]
        for p in paths:
            files += glob.glob(p)
        if not files:
            msg = 'No files found for dataset {!r} with mode {!r} at {!r}'
            raise FileNotFoundError(msg.format(
                self.dataset.name, mode, ', '.join(paths)))
        return files

    def _excepthook(self, etype, evalue, etb):
        from IPython.core import ultratb
        ultratb.FormattedTB()(etype, evalue, etb)
        if etype is KeyboardInterrupt:
            return
        if self.get('system.use_pdb', True):
            import ipdb
            ipdb.post_mortem(etb)

    def _setup_excepthook(self):
        sys.excepthook = self._excepthook

    def _setup_log_level(self):
        level = self.get('system.log_level.mayo', 'info')
        if level != 'info':
            from mayo.log import log
            log.level = level
        level = self.get('system.log_level.tensorflow', 0)
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = str(level)
