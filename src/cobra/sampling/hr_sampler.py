# -*- coding: utf-8 -*-

"""Provide base class for Hit-and-Run samplers.

New samplers should derive from the abstract `HRSampler` class
where possible to provide a uniform interface."""

from __future__ import absolute_import, division

import ctypes
from collections import namedtuple
from logging import getLogger
from multiprocessing import Array
from time import time

import numpy as np
from optlang.interface import OPTIMAL
from optlang.symbolics import Zero

from cobra.util import constraint_matrices, create_stoichiometric_matrix, nullspace


LOGGER = getLogger(__name__)


# Maximum number of retries for sampling
MAX_TRIES = 100


Problem = namedtuple(
    "Problem",
    [
        "equalities",
        "b",
        "inequalities",
        "bounds",
        "variable_fixed",
        "variable_bounds",
        "nullspace",
        "homogeneous",
    ],
)
"""Defines the matrix representation of a sampling problem.

Attributes
----------
equalities : numpy.array
    All equality constraints in the model.
b : numpy.array
    The right side of the equality constraints.
inequalities : numpy.array
    All inequality constraints in the model.
bounds : numpy.array
    The lower and upper bounds for the inequality constraints.
variable_bounds : numpy.array
    The lower and upper bounds for the variables.
homogeneous: boolean
    Indicates whether the sampling problem is homogenous, e.g. whether there
    exist no non-zero fixed variables or constraints.
nullspace : numpy.matrix
    A matrix containing the nullspace of the equality constraints. Each column
    is one basis vector.

"""


def shared_np_array(shape, data=None, integer=False):
    """Create a new numpy array that resides in shared memory.

    Parameters
    ----------
    shape : tuple of ints
        The shape of the new array.
    data : numpy.array
        Data to copy to the new array. Has to have the same shape.
    integer : boolean
        Whether to use an integer array. Defaults to False which means
        float array.

    """

    size = np.prod(shape)

    if integer:
        array = Array(ctypes.c_int64, int(size))
        np_array = np.frombuffer(array.get_obj(), dtype="int64")
    else:
        array = Array(ctypes.c_double, int(size))
        np_array = np.frombuffer(array.get_obj())

    np_array = np_array.reshape(shape)

    if data is not None:
        if len(shape) != len(data.shape):
            raise ValueError(
                "`data` must have the same dimensions" "as the created array."
            )
        same = all(x == y for x, y in zip(shape, data.shape))

        if not same:
            raise ValueError("`data` must have the same shape" "as the created array.")
        np_array[:] = data

    return np_array


class HRSampler(object):
    """The abstract base class for hit-and-run samplers.

    Parameters
    ----------
    model : cobra.Model
        The cobra model from which to generate samples.
    thinning : int
        The thinning factor of the generated sampling chain. A thinning of 10
        means samples are returned every 10 steps.
    nproj : int > 0, optional
        How often to reproject the sampling point into the feasibility space.
        Avoids numerical issues at the cost of lower sampling. If you observe
        many equality constraint violations with `sampler.validate` you should
        lower this number.
    seed : int > 0, optional
        The random number seed that should be used.

    Attributes
    ----------
    model : cobra.Model
        The cobra model from which the sampes get generated.
    feasibility_tol: float
        The tolerance used for checking equalities feasibility.
    val_feasibility_tol: float
        The tolerance used for checking equalities feasibility but just for validate function.
    bounds_tol: float
        The tolerance used for checking bounds feasibility.
    val_bounds_tol: float
        The tolerance used for checking bounds feasibility but just for validate function.
    thinning : int
        The currently used thinning factor.
    n_samples : int
        The total number of samples that have been generated by this
        sampler instance.
    retries : int
        The overall of sampling retries the sampler has observed. Larger
        values indicate numerical instabilities.
    problem : collections.namedtuple
        A python object whose attributes define the entire sampling problem in
        matrix form. See docstring of `Problem`.
    warmup : numpy.matrix
        A matrix of with as many columns as reactions in the model and more
        than 3 rows containing a warmup sample in each row. None if no warmup
        points have been generated yet.
    nproj : int
        How often to reproject the sampling point into the feasibility space.
    seed : int > 0, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.
    fwd_idx : numpy.array
        Has one entry for each reaction in the model containing the index of
        the respective forward variable.
    rev_idx : numpy.array
        Has one entry for each reaction in the model containing the index of
        the respective reverse variable.

    """

    def __init__(self, model, thinning, nproj=None, seed=None):
        """Initialize a new sampler object."""

        # This currently has to be done to reset the solver basis which is
        # required to get deterministic warmup point generation
        # (in turn required for a working `seed` arg)
        if model.solver.is_integer:
            raise TypeError("sampling does not work with integer problems :(")

        self.model = model.copy()
        self.feasibility_tol = model.tolerance
        self.bounds_tol = model.tolerance
        self.thinning = thinning

        if nproj is None:
            self.nproj = int(min(len(self.model.variables) ** 3, 1e6))
        else:
            self.nproj = nproj

        self.n_samples = 0
        self.retries = 0
        self.problem = self.__build_problem()

        # Set up a map from reaction -> forward/reverse variable
        var_idx = {v: idx for idx, v in enumerate(self.model.variables)}

        self.fwd_idx = np.array(
            [var_idx[r.forward_variable] for r in self.model.reactions]
        )
        self.rev_idx = np.array(
            [var_idx[r.reverse_variable] for r in self.model.reactions]
        )
        self.warmup = None

        if seed is None:
            self._seed = int(time())
        else:
            self._seed = seed

        # Avoid overflow
        self._seed = self._seed % np.iinfo(np.int32).max

    def __build_problem(self):
        """Build the matrix representation of the sampling problem."""

        # Set up the mathematical problem
        prob = constraint_matrices(self.model, zero_tol=self.feasibility_tol)

        # check if there any non-zero equality constraints
        equalities = prob.equalities
        b = prob.b
        bounds = np.atleast_2d(prob.bounds).T
        var_bounds = np.atleast_2d(prob.variable_bounds).T
        homogeneous = all(np.abs(b) < self.feasibility_tol)
        fixed_non_zero = np.abs(prob.variable_bounds[:, 1]) > self.feasibility_tol
        fixed_non_zero &= prob.variable_fixed

        # check if there are any non-zero fixed variables, add them as
        # equalities to the stoichiometric matrix
        if any(fixed_non_zero):
            n_fixed = fixed_non_zero.sum()
            rows = np.zeros((n_fixed, prob.equalities.shape[1]))
            rows[range(n_fixed), np.where(fixed_non_zero)] = 1.0
            equalities = np.vstack([equalities, rows])
            var_b = prob.variable_bounds[:, 1]
            b = np.hstack([b, var_b[fixed_non_zero]])
            homogeneous = False

        # Set up a projection that can cast point into the nullspace
        nulls = nullspace(equalities)

        # convert bounds to a matrix and add variable bounds as well
        return Problem(
            equalities=shared_np_array(equalities.shape, equalities),
            b=shared_np_array(b.shape, b),
            inequalities=shared_np_array(prob.inequalities.shape, prob.inequalities),
            bounds=shared_np_array(bounds.shape, bounds),
            variable_fixed=shared_np_array(
                prob.variable_fixed.shape, prob.variable_fixed, integer=True
            ),
            variable_bounds=shared_np_array(var_bounds.shape, var_bounds),
            nullspace=shared_np_array(nulls.shape, nulls),
            homogeneous=homogeneous,
        )

    def generate_fva_warmup(self, includeReversible=False):
        """Generate the warmup points for the sampler.

        Generates warmup points by setting each flux as the sole objective
        and minimizing/maximizing it. Also caches the projection of the
        warmup points into the nullspace for non-homogeneous problems (only
        if necessary).

        Parameters
        ----------
        includeReversible : boolean
            Whether to include warmup samples that move in the direction
            of altering forward and backward fluxes, without altering net flux
            (e.g. altering circulation or exchange flux). This is useful for
            samplers fitting to 13C MFA (Metabolic Flux Analysis) data, where
            reverse fluxes are considered. If set to False, the warmup points
            will only allow the sampler to move in directions that change
            the net flux of reactions.

        """

        self.n_warmup = 0
        reactions = self.model.reactions
        if includeReversible:
            # make warmup matrix big enough to include reversible reactions
            print('sum',sum([r.reversibility for r in reactions]))
            print('len', len([r.reversibility for r in reactions]))
            warmupPoints = 2 * (len(reactions) + sum([r.reversibility for r in reactions]))
        else:
            warmupPoints = 2 * len(reactions)
        self.warmup = np.zeros((warmupPoints, len(self.model.variables)))
        self.model.objective = Zero

        for sense in ("min", "max"):
            self.model.objective_direction = sense

            for i, r in enumerate(reactions):
                variables = (
                    self.model.variables[self.fwd_idx[i]],
                    self.model.variables[self.rev_idx[i]],
                )

                # Omit fixed reactions if they are non-homogeneous
                if r.upper_bound - r.lower_bound < self.bounds_tol:
                    LOGGER.info("skipping fixed reaction %s" % r.id)
                    continue

                self.model.objective.set_linear_coefficients(
                    {variables[0]: 1, variables[1]: -1}
                )

                self.model.slim_optimize()

                if not self.model.solver.status == OPTIMAL:
                    LOGGER.info("can not maximize reaction %s, skipping it" % r.id)
                    continue

                primals = self.model.solver.primal_values
                sol = [primals[v.name] for v in self.model.variables]
                self.warmup[self.n_warmup,] = sol
                self.n_warmup += 1

                # Reset objective
                self.model.objective.set_linear_coefficients(
                    {variables[0]: 0, variables[1]: 0}
                )

                # If reaction is reversible and includeReversible=True
                # we will consider directions which maximize circulation
                # flux by maximizing both the forward and reverse directions
                # of the same reaction.
                if includeReversible and r.reversibility:
                    ## Omit fixed reactions if they are non-homogeneous
                    #if r.upper_bound - r.lower_bound < self.bounds_tol:
                        #LOGGER.info("skipping fixed reaction %s" % r.id)
                        #continue

                    # both coefficients are positive to maximize
                    # circulation within this reaction
                    self.model.objective.set_linear_coefficients(
                        {variables[0]: 1, variables[1]: 1})
                    self.model.slim_optimize()
                    if not self.model.solver.status == OPTIMAL:
                        LOGGER.info("can not maximize reaction %s, skipping it" %
                                    r.id)
                        continue
                    primals = self.model.solver.primal_values
                    sol = [primals[v.name] for v in self.model.variables]
                    self.warmup[self.n_warmup, ] = sol
                    self.n_warmup += 1
                    # Reset objective
                    self.model.objective.set_linear_coefficients(
                        {variables[0]: 0, variables[1]: 0})

        # Shrink to measure
        self.warmup = self.warmup[0 : self.n_warmup, :]

        # Remove redundant search directions
        keep = np.logical_not(self._is_redundant(self.warmup))
        self.warmup = self.warmup[keep, :]
        self.n_warmup = self.warmup.shape[0]

        # Catch some special cases
        if len(self.warmup.shape) == 1 or self.warmup.shape[0] == 1:
            raise ValueError("Your flux cone consists only of a single point!")
        elif self.n_warmup == 2:
            if not self.problem.homogeneous:
                raise ValueError(
                    "Can not sample from an inhomogenous problem"
                    " with only 2 search directions :("
                )
            LOGGER.info("All search directions on a line, adding another one.")
            newdir = self.warmup.T.dot([0.25, 0.25])
            self.warmup = np.vstack([self.warmup, newdir])
            self.n_warmup += 1

        # Shrink warmup points to measure
        self.warmup = shared_np_array(
            (self.n_warmup, len(self.model.variables)), self.warmup
        )

    def _reproject(self, p):
        """Reproject a point into the feasibility region.

        This function is guaranteed to return a new feasible point. However,
        no guarantees in terms of proximity to the original point can be made.

        Parameters
        ----------
        p : numpy.array
            The current sample point.

        Returns
        -------
        numpy.array
            A new feasible point. If `p` was feasible it wil return p.

        """

        nulls = self.problem.nullspace
        equalities = self.problem.equalities

        # don't reproject if point is feasible
        if np.allclose(
            equalities.dot(p), self.problem.b, rtol=0, atol=self.feasibility_tol
        ):
            new = p
        else:
            LOGGER.info(
                "feasibility violated in sample"
                " %d, trying to reproject" % self.n_samples
            )
            new = nulls.dot(nulls.T.dot(p))

        # Projections may violate bounds
        # set to random point in space in that case
        if any(new != p):
            LOGGER.info(
                "reprojection failed in sample"
                " %d, using random point in space" % self.n_samples
            )
            new = self._random_point()

        return new

    def _random_point(self):
        """Find an approximately random point in the flux cone."""

        idx = np.random.randint(
            self.n_warmup, size=min(2, np.ceil(np.sqrt(self.n_warmup)))
        )
        return self.warmup[idx, :].mean(axis=0)

    def _is_redundant(self, matrix, cutoff=None):
        """Identify rdeundant rows in a matrix that can be removed."""

        cutoff = 1.0 - self.feasibility_tol

        # Avoid zero variances
        extra_col = matrix[:, 0] + 1

        # Avoid zero rows being correlated with constant rows
        extra_col[matrix.sum(axis=1) == 0] = 2
        corr = np.corrcoef(np.c_[matrix, extra_col])
        corr = np.tril(corr, -1)

        return (np.abs(corr) > cutoff).any(axis=1)

    def _bounds_dist(self, p):
        """Get the lower and upper bound distances. Negative is bad."""

        prob = self.problem
        lb_dist = (p - prob.variable_bounds[0,]).min()
        ub_dist = (prob.variable_bounds[1,] - p).min()

        if prob.bounds.shape[0] > 0:
            const = prob.inequalities.dot(p)
            const_lb_dist = (const - prob.bounds[0,]).min()
            const_ub_dist = (prob.bounds[1,] - const).min()
            lb_dist = min(lb_dist, const_lb_dist)
            ub_dist = min(ub_dist, const_ub_dist)

        return np.array([lb_dist, ub_dist])

    def sample(self, n, fluxes=True):
        """Abstract sampling function.

        Should be overwritten by child classes.

        """
        pass

    def batch(self, batch_size, batch_num, fluxes=True):
        """Create a batch generator.

        This is useful to generate n batches of m samples each.

        Parameters
        ----------
        batch_size : int
            The number of samples contained in each batch (m).
        batch_num : int
            The number of batches in the generator (n).
        fluxes : boolean
            Whether to return fluxes or the internal solver variables. If set
            to False will return a variable for each forward and backward flux
            as well as all additional variables you might have defined in the
            model.

        Yields
        ------
        pandas.DataFrame
            A DataFrame with dimensions (batch_size x n_r) containing
            a valid flux sample for a total of n_r reactions (or variables if
            fluxes=False) in each row.

        """

        for i in range(batch_num):
            yield self.sample(batch_size, fluxes=fluxes)

    def validate(self, samples, feas_tol=None, bounds_tol=None):
        """Validate a set of samples for equality and inequality feasibility.

        Can be used to check whether the generated samples and warmup points
        are feasible.

        Parameters
        ----------
        samples : numpy.matrix
            Must be of dimension (n_samples x n_reactions). Contains the
            samples to be validated. Samples must be from fluxes.

        Returns
        -------
        numpy.array
            A one-dimensional numpy array of length containing
            a code of 1 to 3 letters denoting the validation result:

            - 'v' means feasible in bounds and equality constraints
            - 'l' means a lower bound violation
            - 'u' means a lower bound validation
            - 'e' means and equality constraint violation

        """

        # introduce new parameter feas_tol for equality constraints, such that it can be user-defined and for MCMCACHRSampler class has new default value but just for validate function
        if feas_tol is not None and feas_tol!=1e-7:
            self.val_feasibility_tol = feas_tol  # if user provided set to user tolerance
            #print('Different feasibility tolerance for validate function was provided, i.e.', feas_tol)
        elif feas_tol==1e-7:
            self.val_feasibility_tol = feas_tol
        else:
            self.val_feasibility_tol = 1e-6  # else set to 1e-6 instead of model.tolerance=1e-7 from cobrapy (instead of self.feasibility_tol)

        # introduce new parameter bounds_tol for inequality constraints, such that it can be user-defined and for MCMCACHRSampler class has new default value but just for validate function
        if bounds_tol is not None and bounds_tol!=1e-7:
            self.val_bounds_tol = bounds_tol  # if user provided set to user tolerance
            #print('Different bounds tolerance for validate function was provided, i.e.', bounds_tol)
        elif bounds_tol==1e-7:
            self.val_bounds_tol = bounds_tol
        else:
            self.val_bounds_tol = 1e-6 # else set to 1e-6 instead of model.tolerance=1e-7 from cobrapy (instead of self.bounds_tol)

        samples = np.atleast_2d(samples)
        prob = self.problem

        if samples.shape[1] == len(self.model.reactions):
            S = create_stoichiometric_matrix(self.model)
            b = np.array(
                [self.model.constraints[m.id].lb for m in self.model.metabolites]
            )
            bounds = np.array([r.bounds for r in self.model.reactions]).T
        elif samples.shape[1] == len(self.model.variables):
            S = prob.equalities
            b = prob.b
            bounds = prob.variable_bounds
        else:
            raise ValueError(
                "Wrong number of columns. samples must have a "
                "column for each flux or variable defined in the "
                "model!"
            )

        feasibility = np.abs(S.dot(samples.T).T - b).max(axis=1)
        #print('feas', feasibility.shape)
        lb_error = (samples - bounds[0,]).min(axis=1)
        ub_error = (bounds[1,] - samples).min(axis=1)

        #if samples.shape[1] == len(self.model.variables) and prob.inequalities.shape[0]:

        #print(prob.inequalities.shape[0])
        if samples.shape[1] == len(self.model.variables) and prob.inequalities.shape[0]:

            #Original:
            consts = prob.inequalities.dot(samples.T)
            print('const', type(consts))
            print(type(prob.bounds[0,]))
            print(type(prob.bounds[1,]))
            #print('before', np.minimum(lb_error, (consts - prob.bounds[0,]).min(axis=1)))
            #consts = consts.reshape((consts.shape[0],))
            #print(prob.bounds[0,].shape)
            try:
                lb_error = np.minimum(lb_error, (consts - prob.bounds[0,]).min(axis=1))
            except IndexError:
                lb_error = np.minimum(lb_error, (consts - prob.bounds[0,]).min())
            try:
                ub_error = np.minimum(ub_error, (prob.bounds[1,] - consts).min(axis=1))
            except IndexError:
                ub_error = np.minimum(ub_error, (prob.bounds[1,] - consts).min())

            #print('after', np.minimum(lb_error, (consts - prob.bounds[0,]).min(axis=0)))#axis=1))
            #lb_error = lb_error.reshape((lb_error.shape[0],1))
            #ub_error = ub_error.reshape((ub_error.shape[0],1))

        #valid = (
        #(feasibility < self.feasibility_tol)
        #& (lb_error > -self.bounds_tol)
        #& (ub_error > -self.bounds_tol)
        #)
        #codes = np.repeat("", valid.shape[0]).astype(np.dtype((str, 3)))
        #codes[valid] = "v"
        #codes[lb_error <= -self.bounds_tol] = np.char.add(
            #codes[lb_error <= -self.bounds_tol], "l"
        #)
        #codes[ub_error <= -self.bounds_tol] = np.char.add(
            #codes[ub_error <= -self.bounds_tol], "u"
        #)
        #codes[feasibility > self.feasibility_tol] = np.char.add(
            #codes[feasibility > self.feasibility_tol], "e"
        #)
        ##if np.any(feasibility) > self.feasibility_tol:
            ##print('feasibility', feasibility)
        valid = (
            (feasibility < self.val_feasibility_tol)
            & (lb_error > -self.val_bounds_tol)
            & (ub_error > -self.val_bounds_tol)
        )
        codes = np.repeat("", valid.shape[0]).astype(np.dtype((str, 3)))
        codes[valid] = "v"
        codes[lb_error <= -self.val_bounds_tol] = np.char.add(
            codes[lb_error <= -self.val_bounds_tol], "l"
        )
        #print(ub_error.shape)
        codes[ub_error <= -self.val_bounds_tol] = np.char.add(
            codes[ub_error <= -self.val_bounds_tol], "u"
        )
        #print('feas', feasibility.shape)
        try:
            codes[feasibility > self.val_feasibility_tol] = np.char.add(
                codes[feasibility > self.val_feasibility_tol], "e"
            )
        except IndexError:
            feasibility=feasibility.max()
            codes[feasibility > self.val_feasibility_tol] = np.char.add(
                codes[feasibility > self.val_feasibility_tol], "e"
            )
        return codes


        ######################3
        #####Original +modification because of constraints##########

        #samples = np.atleast_2d(samples)
        #prob = self.problem

        #if samples.shape[1] == len(self.model.reactions):
            #S = create_stoichiometric_matrix(self.model)
            #b = np.array(
                #[self.model.constraints[m.id].lb for m in self.model.metabolites]
            #)
            #bounds = np.array([r.bounds for r in self.model.reactions]).T
        #elif samples.shape[1] == len(self.model.variables):
            #S = prob.equalities
            #b = prob.b
            #bounds = prob.variable_bounds
        #else:
            #raise ValueError(
                #"Wrong number of columns. Samples must have a "
                #"column for each flux or variable defined in the "
                #"model."
            #)

        #feasibility = np.abs(S.dot(samples.T).T - b).max(axis=1)
        #lb_error = (
            #samples
            #- bounds[
                #0,
            #]
        #).min(axis=1)
        #ub_error = (
            #bounds[
                #1,
            #]
            #- samples
        #).min(axis=1)

        ##print(prob.inequalities.shape[0])
        #if samples.shape[1] == len(self.model.variables) and prob.inequalities.shape[0]:
            #print(prob.bounds[0, ])
            #consts = prob.inequalities.dot(samples.T)

            ##added and changed:
            ##print(prob.bounds[0, ].shape)
            ##print(consts.shape)
            ##print(lb_error.shape)
            #diff0 = np.zeros(consts.shape)
            #diff1 = np.zeros(consts.shape)
            #for i in range(0,len(prob.bounds[0, ])):
                #if prob.bounds[0, i]!=None or prob.bounds[0, i]!=False:
                    #diff0[i,:] = consts[i] - prob.bounds[0, i]
                #else:
                    #diff0[i,:] = lb_error[i]
                #if prob.bounds[1, i]!=None or prob.bounds[1, i]!=False:
                    #diff1[i,:] = prob.bounds[1, i] - consts[i]
                #else:
                    #diff1[i,:] = ub_error[i]

            #lb_error = np.minimum(
                #lb_error,
                #(
                    #diff0
                #).min(axis=0),
            #)
        ##if prob.bounds[1, ]:
            #ub_error = np.minimum(
                #ub_error,
                #(
                    #diff1
                #).min(axis=0),
            #)

            ##lb_error = np.minimum(
                ##lb_error,
                ##(
                    ##consts
                    ##- prob.bounds[
                        ##0,
                    ##]
                ##).min(axis=1),
            ##)
        ###if prob.bounds[1, ]:
            ##ub_error = np.minimum(
                ##ub_error,
                ##(
                    ##prob.bounds[
                        ##1,
                    ##]
                    ##- consts
                ##).min(axis=1),
            ##)


        #valid = (
            #(feasibility < self.feasibility_tol)
            #& (lb_error > -self.bounds_tol)
            #& (ub_error > -self.bounds_tol)
        #)
        #codes = np.repeat("", valid.shape[0]).astype(np.dtype((str, 3)))
        #codes[valid] = "v"
        #codes[lb_error <= -self.bounds_tol] = np.char.add(
            #codes[lb_error <= -self.bounds_tol], "l"
        #)
        #codes[ub_error <= -self.bounds_tol] = np.char.add(
            #codes[ub_error <= -self.bounds_tol], "u"
        #)
        #codes[feasibility > self.feasibility_tol] = np.char.add(
            #codes[feasibility > self.feasibility_tol], "e"
        #)
        ##if np.any(feasibility) > self.feasibility_tol:
            ##print('feasibility', feasibility)


        #return codes


# Required by ACHRSampler and OptGPSampler
# Has to be declared outside of class to be used for multiprocessing :(
def step(sampler, x, delta, fraction=None, tries=0):
    """Sample a new feasible point from the point `x` in direction `delta`."""

    prob = sampler.problem
    valid = (np.abs(delta) > sampler.feasibility_tol) & np.logical_not(
        prob.variable_fixed
    )

    # permissible alphas for staying in variable bounds
    valphas = ((1.0 - sampler.bounds_tol) * prob.variable_bounds - x)[:, valid]
    valphas = (valphas / delta[valid]).flatten()

    if prob.bounds.shape[0] > 0:
        # permissible alphas for staying in constraint bounds
        ineqs = prob.inequalities.dot(delta)
        valid = np.abs(ineqs) > sampler.feasibility_tol
        balphas = ((1.0 - sampler.bounds_tol) * prob.bounds - prob.inequalities.dot(x))[
            :, valid
        ]
        balphas = (balphas / ineqs[valid]).flatten()

        # combined alphas
        alphas = np.hstack([valphas, balphas])
    else:
        alphas = valphas
    pos_alphas = alphas[alphas > 0.0]
    neg_alphas = alphas[alphas <= 0.0]
    alpha_range = np.array(
        [
            neg_alphas.max() if len(neg_alphas) > 0 else 0,
            pos_alphas.min() if len(pos_alphas) > 0 else 0,
        ]
    )

    if fraction:
        alpha = alpha_range[0] + fraction * (alpha_range[1] - alpha_range[0])
    else:
        alpha = np.random.uniform(alpha_range[0], alpha_range[1])

    #print(alpha_range, alpha)

    p = x + alpha * delta

    # Numerical instabilities may cause bounds invalidation
    # reset sampler and sample from one of the original warmup directions
    # if that occurs. Also reset if we got stuck.
    if (
        np.any(sampler._bounds_dist(p) < -sampler.bounds_tol)
        or np.abs(np.abs(alpha_range).max() * delta).max() < sampler.bounds_tol
    ):
        if tries > MAX_TRIES:
            raise RuntimeError(
                "Can not escape sampling region, model seems"
                " numerically unstable :( Reporting the "
                "model to "
                "https://github.com/opencobra/cobrapy/issues "
                "will help us to fix this :)"
            )
        LOGGER.info("found bounds infeasibility in sample, " "resetting to center")
        newdir = sampler.warmup[np.random.randint(sampler.n_warmup)]
        sampler.retries += 1

        return step(sampler, sampler.center, newdir - sampler.center, None, tries + 1)
    return p
