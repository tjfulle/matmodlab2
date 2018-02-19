import logging
import numpy as np
from copy import deepcopy as copy

from .misc import add_metaclass, is_scalarlike, is_listlike
from .deformation import defgrad_from_strain
from .database import COMPONENT_SEP
from .tensor import SYMMETRIC_COMPONENTS, TENSOR_COMPONENTS

class BaseMaterial(type):
    def __call__(cls, *args, **kwargs):
        """Called before __init__ method is called"""

        # Call the objects __init__ method (indirectly through __call__)
        obj = type.__call__(cls, *args, **kwargs)

        # Store the addon models
        obj.addon_models = []

        expansion_model = kwargs.pop('expansion_model', None)
        if expansion_model is not None:
            expansion_model = ExpansionModel(expansion_model)
            obj.addon_models.append(expansion_model)

        porepres = kwargs.pop('pore_pres', None)
        if porepres is not None:
            effstress_model = EffectiveStressModel(porepres)
            obj.addon_models.append(effstress_model)

        return obj

@add_metaclass(BaseMaterial)
class Material(object):
    """The material model base class

    Notes
    -----
    The `Material` class is a base class and is meant to be inherited by
    concrete implementations of material models. At minimum, the material model
    must provide an `eval` method that is called by the model driver to update
    the material state. See the documentation for `eval` for more details.

    For material models that require state dependent variable tracking, the
    `num_sdv` member must be set to the number of state dependent variables
    required. Optionally, the `sdv_names` member can also be set to a list of
    state dependent variable names (for output purposes). State dependent
    variables are initialized to 0. The method `sdvini` can optionally be
    defined that returns alternative values for state dependent variables. See
    the documentation for `sdvini` for more information.

    """
    name = None
    num_sdv = None
    sdv_names = None
    _has_addon_models = None

    def sdvini(self, statev):
        """Initialize the state dependent variables

        Parameters
        ----------
        statev : ndarray or None
            If `self.num_sdv is None` than `statev` is also `None`, otherwise
            it an array of zeros `self.num_sdv` in length

        Returns
        -------
        statev : ndarray or None
            The initialized state dependent variables.

        Notes
        -----
        This base method does not need to be overwritten if a material does not
        have any state dependent variables, or their initial values should be
        zero.

        """
        return statev

    @property
    def has_addon_models(self):
        if self._has_addon_models is None:
            self._has_addon_models = hasattr(self, 'addon_models')
        return self._has_addon_models

    def base_eval(self, kappa, time, dtime, temp, dtemp,
                  F0, F, strain, d, stress, ufield, dufield, statev,
                  initial_temp, **kwds):
        """Wrapper method to material.eval. This is called by Matmodlab so that
        addon models can first be evaluated. See documentation for eval.

        """
        num_sdv = getattr(self, 'num_sdv', None)
        i = 0 if num_sdv is None else num_sdv

        if hasattr(self, 'addon_models'):
            for model in self.addon_models:
                # Evaluate each addon model.  Each model must change the input
                # arrays in place.

                # Determine starting point in statev array
                j = i + model.num_sdv
                xv = statev[i:j]
                model.eval(kappa, time, dtime, temp, dtemp,
                           F0, F, strain, d, stress, xv,
                           initial_temp=initial_temp,
                           ufield=ufield, dufield=dufield, **kwds)
                statev[i:j] = xv
                i += j

        if num_sdv is not None:
            xv = statev[:num_sdv]
        else:
            xv = None
        sig, xv, ddsdde = self.eval(time, dtime, temp, dtemp, F0, F, strain, d,
                                    stress, xv, ufield=ufield, dufield=dufield,
                                    **kwds)

        if hasattr(self, 'addon_models'):
            for model in self.addon_models:
                # Determine starting point in statev array
                j = i + model.num_sdv
                xv = statev[i:j]
                model.posteval(kappa, time, dtime, temp, dtemp,
                               F0, F, strain, d, sig, xv,
                               initial_temp=initial_temp,
                               ufield=ufield, dufield=dufield, **kwds)
                statev[i:j] = xv
                i += j

        if num_sdv is not None:
            statev[:num_sdv] = xv

        return sig, statev, ddsdde

    def eval(self, time, dtime, temp, dtemp,
             F0, F, strain, d, stress, statev, **kwds):
        """Evaluate the material model

        Parameters
        ----------
        time : float
            Time at beginning of step
        dtime : float
            Time step length.  `time+dtime` is the time at the end of the step
        temp : float
            Temperature at beginning of step
        dtemp : float
            Temperature increment. `temp+dtemp` is the temperature at the end
            of the step
        F0, F : ndarray
            Deformation gradient at the beginning and end of the step
        strain : ndarray
            Strain at the beginning of the step
        d : ndarray
            Symmetric part of the velocity gradient at the middle of the step
        stress : ndarray
            Stress at the beginning of the step
        statev : ndarray
            State variables at the beginning of the step

        Returns
        -------
        stress : ndarray
            Stress at the end of the step
        statev : ndarray
            State variables at the end of the step
        ddsdde : ndarray
            Elastic stiffness (Jacobian) of the material

        Notes
        -----
        Each material model is responsible for returning the elastic stiffness.
        If an analytic elastic stiffness is not known, return `None` and it
        will be computed numerically.

        The input arrays `stress` and `statev` are mutable and copies are not
        passed in. DO NOT MODIFY THEM IN PLACE. Doing so can cause problems
        down stream.

        """
        raise NotImplementedError

class AddonModel(object):
    def sdvini(self, statev):
        return statev
    def eval(self, kappa, time, dtime, temp, dtemp, F0, F, strain, d,
             stress, statev, initial_temp=0., **kwds):
        # All arrays must be modified in place
        return None
    def posteval(self, kappa, time, dtime, temp, dtemp, F0, F, strain, d,
                 stress, statev, initial_temp=0., **kwds):
        # All arrays must be modified in place
        return None


class ExpansionModel(AddonModel):
    """Thermal expansion model"""
    def __init__(self, expansion):
        """Format the thermal expansion term"""
        self.num_sdv = 15
        self.sdv_names = ['EM'+COMPONENT_SEP+x for x in SYMMETRIC_COMPONENTS]
        self.sdv_names.extend(['FM'+COMPONENT_SEP+x for x in TENSOR_COMPONENTS])
        if is_scalarlike(expansion):
            expansion = [expansion] * 3
        if not is_listlike(expansion):
            raise ValueError('Expected expansion to be array_like')
        if len(expansion) == 3:
            expansion = [x for x in expansion] + [0, 0, 0]
        if len(expansion) != 6:
            raise ValueError('Expected len(expansion) to be 3 or 6')
        self.data = np.array([float(x) for x in expansion])

    def sdvini(self, statev):
        statev = np.append(np.zeros(6), np.array([1.,0.,0.,0.,1.,0.,0.,0.,1.]))
        return statev

    def eval(self, kappa, time, dtime, temp, dtemp,
             F0, F, strain, d, stress, statev, initial_temp=0., **kwds):
        """Evaluate the thermal expansion model

        F0, F, strain, d are updated in place
        """
        assert len(statev) == 15

        # Determine mechanical strain
        thermal_strain = (temp + dtemp - initial_temp) * self.data
        strain -= thermal_strain

        # Updated deformation gradient
        F0[:9] = np.array(statev[6:15])
        F[:9] = defgrad_from_strain(strain, kappa, flatten=1)

        thermal_d = self.data * dtemp / dtime
        d -= thermal_d

        # Save the mechanical state to the statev
        statev[:self.num_sdv] = np.append(strain, F)

        return None

class EffectiveStressModel(AddonModel):
    """Effective stress model"""
    def __init__(self, porepres):
        """Format the thermal expansion term"""
        self.num_sdv = 1
        self.sdv_names = ['POREPRES']
        self.porepres = np.asarray(porepres)
        if len(self.porepres.shape) != 2:
            raise ValueError('pore_pres must be a 2 dimensional array with '
                             'the first column being time and the second the '
                             'associated pore pressure.')
    def get_porepres_at_time(self, time):
        return np.interp(1, self.porepres[0,:], self.porepres[1,:],
                         left=self.porepres[1,0], right=self.porepres[1,-1])
    def sdvini(self, statev):
        statev = np.array([self.get_porepres_at_time(0.)])
        return statev

    def eval(self, kappa, time, dtime, temp, dtemp,
             F0, F, strain, d, stress, statev, **kwds):
        """Evaluate the effective stress model

        """
        porepres = self.get_porepres_at_time(time+dtime/2.)
        stress[[0,1,2]] -= porepres
        statev[0] = porepres
        return None

    def posteval(self, kappa, time, dtime, temp, dtemp, F0, F, strain, d,
                 stress, statev, **kwds):
        porepres = self.get_porepres_at_time(time+dtime/2.)
        stress[[0,1,2]] += porepres
        statev[0] = porepres
        return None

