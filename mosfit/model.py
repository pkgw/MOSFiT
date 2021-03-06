import importlib
import json
import logging
import os
from collections import OrderedDict
from math import isnan

import numpy as np
# from bayes_opt import BayesianOptimization
from mosfit.constants import LOCAL_LIKELIHOOD_FLOOR
from mosfit.utils import listify, print_wrapped
# from scipy.optimize import differential_evolution
from scipy.optimize import minimize


class Model:
    """Define a semi-analytical model to fit transients with.
    """

    MODEL_OUTPUT_DIR = 'products'

    class outClass(object):
        pass

    def __init__(self,
                 parameter_path='parameters.json',
                 model='default',
                 wrap_length=100,
                 pool=None):
        self._model_name = model
        self._pool = pool
        self._is_master = pool.is_master() if pool else False
        self._wrap_length = wrap_length

        self._dir_path = os.path.dirname(os.path.realpath(__file__))

        # Load the basic model file.
        if os.path.isfile(os.path.join('models', 'model.json')):
            basic_model_path = os.path.join('models', 'model.json')
        else:
            basic_model_path = os.path.join(self._dir_path, 'models',
                                            'model.json')

        with open(basic_model_path, 'r') as f:
            self._model = json.loads(f.read(), object_pairs_hook=OrderedDict)

        # Load the model file.
        model = self._model_name
        model_dir = self._model_name

        if '.json' in self._model_name:
            model_dir = self._model_name.split('.json')[0]
        else:
            model = self._model_name + '.json'

        if os.path.isfile(model):
            model_path = model
        else:
            # Look in local hierarchy first
            if os.path.isfile(os.path.join('models', model_dir, model)):
                model_path = os.path.join('models', model_dir, model)
            else:
                model_path = os.path.join(self._dir_path, 'models', model_dir,
                                          model)

        with open(model_path, 'r') as f:
            self._model.update(
                json.loads(
                    f.read(), object_pairs_hook=OrderedDict))

        # Load model parameter file.
        model_pp = os.path.join(
            os.path.split(model_path)[0], 'parameters.json')

        pp = ''

        selected_pp = os.path.join(
            os.path.split(model_path)[0], parameter_path)

        # First try user-specified path
        if parameter_path and os.path.isfile(parameter_path):
            pp = parameter_path
        # Then try directory we are running from
        elif os.path.isfile('parameters.json'):
            pp = 'parameters.json'
        # Then try the model directory, with the user-specified name
        elif os.path.isfile(selected_pp):
            pp = selected_pp
        # Finally try model folder
        elif os.path.isfile(model_pp):
            pp = model_pp
        else:
            raise ValueError('Could not find parameter file!')

        if self._is_master:
            print_wrapped('Basic model file: ' + basic_model_path, wrap_length)
            print_wrapped('Model file: ' + model_path, wrap_length)
            print_wrapped('Parameter file: ' + pp + '\n', wrap_length)

        with open(pp, 'r') as f:
            self._parameter_json = json.loads(
                f.read(), object_pairs_hook=OrderedDict)
        self._log = logging.getLogger()
        self._modules = OrderedDict()
        self._bands = []

        # Load the call tree for the model. Work our way in reverse from the
        # observables, first constructing a tree for each observable and then
        # combining trees.
        root_kinds = ['output', 'objective']

        self._trees = OrderedDict()
        self.construct_trees(self._model, self._trees, kinds=root_kinds)

        unsorted_call_stack = OrderedDict()
        self._max_depth_all = -1
        for tag in self._model:
            cur_model = self._model[tag]
            roots = []
            if cur_model['kind'] in root_kinds:
                max_depth = 0
                roots = [cur_model['kind']]
            else:
                max_depth = -1
                for tag2 in self._trees:
                    roots.extend(self._trees[tag2]['roots'])
                    depth = self.get_max_depth(tag, self._trees[tag2],
                                               max_depth)
                    if depth > max_depth:
                        max_depth = depth
                    if depth > self._max_depth_all:
                        self._max_depth_all = depth
            roots = list(set(roots))
            new_entry = cur_model.copy()
            new_entry['roots'] = roots
            if 'children' in new_entry:
                del (new_entry['children'])
            new_entry['depth'] = max_depth
            unsorted_call_stack[tag] = new_entry
        # print(unsorted_call_stack)

        # Currently just have one call stack for all products, can be wasteful
        # if only using some products.
        self._call_stack = OrderedDict()
        for depth in range(self._max_depth_all, -1, -1):
            for task in unsorted_call_stack:
                if unsorted_call_stack[task]['depth'] == depth:
                    self._call_stack[task] = unsorted_call_stack[task]

        for task in self._call_stack:
            cur_task = self._call_stack[task]
            if cur_task[
                    'kind'] == 'parameter' and task in self._parameter_json:
                cur_task.update(self._parameter_json[task])
            class_name = cur_task.get('class', task)
            mod = importlib.import_module(
                '.' + 'modules.' + cur_task['kind'] + 's.' + class_name,
                package='mosfit')
            mod_class = getattr(mod, mod.CLASS_NAME)
            self._modules[task] = mod_class(
                name=task, pool=pool, **cur_task)
            if class_name == 'filters':
                self._bands = self._modules[task].band_names()
            # This is currently not functional for MPI
            # cur_task = self._call_stack[task]
            # class_name = cur_task.get('class', task)
            # mod_path = os.path.join('modules', cur_task['kind'] + 's',
            #                         class_name + '.py')
            # if not os.path.isfile(mod_path):
            #     mod_path = os.path.join(self._dir_path, 'modules',
            #                             cur_task['kind'] + 's',
            #                             class_name + '.py')
            # mod_name = ('mosfit.modules.' + cur_task['kind'] + 's.' +
            # class_name)
            # mod = importlib.machinery.SourceFileLoader(mod_name,
            #                                            mod_path).load_module()
            # mod_class = getattr(mod, mod.CLASS_NAME)
            # if (cur_task['kind'] == 'parameter' and task in
            #         self._parameter_json):
            #     cur_task.update(self._parameter_json[task])
            # self._modules[task] = mod_class(name=task, **cur_task)
            # if class_name == 'filters':
            #     self._bands = self._modules[task].band_names()

    def determine_free_parameters(self, extra_fixed_parameters):
        self._free_parameters = []
        for task in self._call_stack:
            cur_task = self._call_stack[task]
            if (task not in extra_fixed_parameters and
                    cur_task['kind'] == 'parameter' and
                    'min_value' in cur_task and 'max_value' in cur_task and
                    cur_task['min_value'] != cur_task['max_value']):
                self._free_parameters.append(task)
        self._num_free_parameters = len(self._free_parameters)

        for task in reversed(self._call_stack):
            cur_task = self._call_stack[task]
            if 'requests' in cur_task:
                inputs = listify(cur_task.get('inputs', []))
                for i, inp in enumerate(inputs):
                    requests = OrderedDict()
                    parent = ''
                    for par in self._call_stack:
                        if par == inp:
                            parent = par
                    if not parent:
                        self._log.error(
                            "Couldn't find parent task for  {}!".format(inp))
                        raise ValueError
                    reqs = cur_task['requests'][i]
                    for req in reqs:
                        requests[req] = self._modules[task].request(req)
                    self._modules[parent].handle_requests(**requests)

    def frack(self, arg):
        """Perform fracking upon a single walker, using a local minimization
        method.
        """
        x = np.array(arg[0])
        step = 0.2
        seed = arg[1]
        np.random.seed(seed)
        my_choice = np.random.choice(range(3))
        # my_choice = 0
        my_method = ['L-BFGS-B', 'TNC', 'SLSQP'][my_choice]
        opt_dict = {'disp': False}
        if my_method in ['TNC', 'SLSQP']:
            opt_dict['maxiter'] = 100
        elif my_method == 'L-BFGS-B':
            opt_dict['maxfun'] = 5000
        # bounds = [(0.0, 1.0) for y in range(self._num_free_parameters)]
        bounds = list(
            zip(np.clip(x - step, 0.0, 1.0), np.clip(x + step, 0.0, 1.0)))

        bh = minimize(
            self.fprob,
            x,
            method=my_method,
            bounds=bounds,
            tol=1.0e-3,
            options=opt_dict)

        # bounds = list(
        #     zip(np.clip(x - step, 0.0, 1.0), np.clip(x + step, 0.0, 1.0)))
        #
        # bh = differential_evolution(
        #     self.fprob, bounds, disp=True, polish=False, maxiter=10)

        # take_step = self.RandomDisplacementBounds(0.0, 1.0, 0.01)
        # bh = basinhopping(
        #     self.fprob,
        #     x,
        #     disp=True,
        #     niter=10,
        #     take_step=take_step,
        #     minimizer_kwargs={'method': "L-BFGS-B",
        #                       'bounds': bounds})

        # bo = BayesianOptimization(self.boprob, dict(
        #     [('x' + str(i),
        #       (np.clip(x[i] - step, 0.0, 1.0),
        #        np.clip(x[i] + step, 0.0, 1.0)))
        #      for i in range(len(x))]))
        #
        # bo.explore(dict([('x' + str(i), [x[i]]) for i in range(len(x))]))
        #
        # bo.maximize(init_points=0, n_iter=20, acq='ei')
        #
        # bh = self.outClass()
        # bh.x = [x[1] for x in sorted(bo.res['max']['max_params'].items())]
        # bh.fun = -bo.res['max']['max_val']

        # m = Minuit(self.fprob)
        # m.migrad()
        return bh

    def construct_trees(self, d, trees, kinds=[], name='', roots=[], depth=0):
        """Construct call trees for each root.
        """
        for tag in d:
            entry = d[tag].copy()
            new_roots = roots
            if entry['kind'] in kinds or tag == name:
                entry['depth'] = depth
                if entry['kind'] in kinds:
                    new_roots.append(entry['kind'])
                entry['roots'] = list(set(new_roots))
                trees[tag] = entry
                inputs = listify(entry.get('inputs', []))
                for inp in inputs:
                    children = OrderedDict()
                    self.construct_trees(
                        d,
                        children,
                        name=inp,
                        roots=new_roots,
                        depth=depth + 1)
                    trees[tag].setdefault('children', OrderedDict())
                    trees[tag]['children'].update(children)

    def draw_walker(self, test=True):
        """Draw a walker randomly from the full range of all parameters, reject
        walkers that return invalid scores.
        """
        p = None
        while p is None:
            draw = np.random.uniform(
                low=0.0, high=1.0, size=self._num_free_parameters)
            draw = [
                self._modules[self._free_parameters[i]].prior_cdf(x)
                for i, x in enumerate(draw)
            ]
            if not test:
                p = draw
                break
            score = self.likelihood(draw)
            if not isnan(score) and np.isfinite(score):
                p = draw
        return p

    def get_max_depth(self, tag, parent, max_depth):
        """Return the maximum depth a given task is found in a tree.
        """
        for child in parent.get('children', []):
            if child == tag:
                new_max = parent['children'][child]['depth']
                if new_max > max_depth:
                    max_depth = new_max
            else:
                new_max = self.get_max_depth(tag, parent['children'][child],
                                             max_depth)
                if new_max > max_depth:
                    max_depth = new_max
        return max_depth

    def likelihood(self, x):
        """Return score related to maximum likelihood.
        """
        outputs = self.run_stack(x, root='objective')
        return outputs['value']

    def prior(self, x):
        """Return score related to paramater priors.
        """
        prior = 0.0
        for pi, par in enumerate(self._free_parameters):
            lprior = self._modules[par].lnprior_pdf(x[pi])
            prior = prior + lprior
        return prior

    def boprob(self, **kwargs):
        x = []
        for key in sorted(kwargs):
            x.append(kwargs[key])

        l = self.likelihood(x) + self.prior(x)
        if not np.isfinite(l):
            return LOCAL_LIKELIHOOD_FLOOR
        return l

    def fprob(self, x):
        """Return score for fracking.
        """
        l = -(self.likelihood(x) + self.prior(x))
        if not np.isfinite(l):
            return -LOCAL_LIKELIHOOD_FLOOR
        return l

    def run_stack(self, x, root='objective'):
        """Run a stack of modules as defined in the model definition file. Only
        run functions that match the specified root.
        """
        inputs = OrderedDict()
        outputs = OrderedDict()
        pos = 0
        cur_depth = self._max_depth_all
        for task in self._call_stack:
            cur_task = self._call_stack[task]
            if root not in cur_task['roots']:
                continue
            if cur_task['depth'] != cur_depth:
                inputs = outputs
            inputs.update({'root': root})
            cur_depth = cur_task['depth']
            if task in self._free_parameters:
                inputs.update({'fraction': x[pos]})
                inputs.setdefault('fractions', []).append(x[pos])
                pos = pos + 1
            new_outs = self._modules[task].process(**inputs)
            outputs.update(new_outs)

            if cur_task['kind'] == root:
                return outputs
