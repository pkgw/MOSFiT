import numpy as np
from mosfit.modules.module import Module

CLASS_NAME = 'Parameter'


class Parameter(Module):
    """Model parameter that can either be free or fixed.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._max_value = kwargs.get('max_value', None)
        self._min_value = kwargs.get('min_value', None)
        if (self._min_value is not None and self._max_value is not None and
                self._min_value == self._max_value):
            print('Warning: Minimum and maximum values are the same for '
                  '`{}`, treating variable as a fixed parameter.'
                  .format(self._name))
            self._value = self._min_value
            self._min_value, self._max_value = None, None
        self._value = kwargs.get('value', None)
        self._log = kwargs.get('log', False)
        self._latex = kwargs.get('latex', self._name)
        if (self._log and self._min_value is not None and
                self._max_value is not None):
            self._min_value = np.log(self._min_value)
            self._max_value = np.log(self._max_value)

    def latex(self):
        return self._latex

    def lnprior_pdf(self, x):
        return 0.0

    def prior_cdf(self, u):
        return u

    def value(self, f):
        value = np.clip(f *
                        (self._max_value - self._min_value) + self._min_value,
                        self._min_value, self._max_value)
        if self._log:
            value = np.exp(value)
        return value

    def process(self, **kwargs):
        """Initialize a parameter based upon either a fixed value or a
        distribution, if one is defined.
        """
        if (self._name in kwargs or self._min_value is None or
                self._max_value is None):
            # If this parameter is not free and is already set, then skip
            if self._name in kwargs:
                return {}

            value = self._value
        else:
            value = self.value(kwargs['fraction'])

        return {self._name: value}
