# -*- coding: utf-8 -*-

"""Provide MCMC ACHR sampler."""

from __future__ import absolute_import, division

import numpy as np
import pandas

from cobra.sampling.hr_sampler import HRSampler, step

import sys


class MCMCACHRSampler(HRSampler):
    """MCMC Artificial Centering Hit-and-Run sampler.

    ACHR sampler with low memory footprint and good convergence, updated
    for MCMC Bayesian Inference by accepting a prior and likelihood function
    for accepting or rejecting samples via the Metropolis test.

    Parameters
    ----------
    model : cobra.Model
        The cobra model from which to generate samples.
    thinning : int, optional
        The thinning factor of the generated sampling chain. A thinning of 10
        means samples are returned every 10 steps.
    nproj : int > 0, optional
        How often to reproject the sampling point into the feasibility space.
        Avoids numerical issues at the cost of lower sampling. If you observe
        many equality constraint violations with `sampler.validate` you should
        lower this number.
    seed : int > 0, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.

    Attributes
    ----------
    model : cobra.Model
        The cobra model from which the samples get generated.
    thinning : int
        The currently used thinning factor.
    n_samples : int
        The total number of samples that have been generated by this
        sampler instance.
    problem : collections.namedtuple
        A python object whose attributes define the entire sampling problem in
        matrix form. See docstring of `Problem`.
    warmup : numpy.matrix
        A matrix of with as many columns as reactions in the model and more
        than 3 rows containing a warmup sample in each row. None if no warmup
        points have been generated yet.
    retries : int
        The overall of sampling retries the sampler has observed. Larger
        values indicate numerical instabilities.
    seed : int > 0, optional
        Sets the random number seed. Initialized to the current time stamp if
        None.
    nproj : int
        How often to reproject the sampling point into the feasibility space.
    fwd_idx : numpy.array
        Has one entry for each reaction in the model containing the index of
        the respective forward variable.
    rev_idx : numpy.array
        Has one entry for each reaction in the model containing the index of
        the respective reverse variable.
    prev : numpy.array
        The current/last flux sample generated.
    center : numpy.array
        The center of the sampling space as estimated by the mean of all
        previously generated samples.

    Notes
    -----
    ACHR generates samples by choosing new directions from the sampling space's
    center and the warmup points. The implementation used here is the same
    as in the Matlab Cobra Toolbox [2]_ and uses only the initial warmup points
    to generate new directions and not any other previous iterates. This
    usually gives better mixing since the startup points are chosen to span
    the space in a wide manner. This also makes the generated sampling chain
    quasi-markovian since the center converges rapidly.

    Memory usage is roughly in the order of (2 * number reactions)^2
    due to the required nullspace matrices and warmup points. So large
    models easily take up a few GB of RAM.

    This MCMC version of the ACHR sampler has been updated to accept a
    prior and/or likelihood function during sampling, for MCMC Bayesian Inference
    of metabolic fluxes. The sampler should first be 'centered' by not passing
    these function, with sufficient samples for the center to converge.
    Next, re-run the sampler with likelihood and/or prior functions, and
    the Metropolis test will be performed to accept or reject samples
    as appropriate [3]. During sampling with a likelihood and/or prior function,
    the center will remain locked to ensure markovian behavior.

    References
    ----------
    .. [1] Direction Choice for Accelerated Convergence in Hit-and-Run Sampling
       David E. Kaufman Robert L. Smith
       Operations Research 199846:1 , 84-95
       https://doi.org/10.1287/opre.46.1.84
    .. [2] https://github.com/opencobra/cobratoolbox
    .. [3] Equation of State Calculations by Fast Computing Machines
       Nicholas Metropolis et al.
       J. Chem. Phys. 21 (6): 1087.
       https://doi.org/10.1063%2F1.1699114

    """

    def __init__(self, model, thinning=100, nproj=None, seed=None):
        """Initialize a new MCMCACHRSampler."""

        super(MCMCACHRSampler, self).__init__(model, thinning, nproj=nproj, seed=seed)
        self.generate_fva_warmup(includeReversible=True)
        self.prev = self.center = self.warmup.mean(axis=0)
        np.random.seed(self._seed)

        # create a variable to store the best point we sampled
        self.bestSample = None

    def __single_iteration(self, lockCenter=False, validatecheck=False):
        """If lockCenter, do not update the center."""

        nmax = 10 #tries to find new valid sample maximum 10 times for each situation

        pi = np.random.randint(self.n_warmup)

        # mix in the original warmup points to not get stuck
        delta = self.warmup[pi, ] - self.center
        ## create testprev to check if current sample is valid
        testprev = step(self, self.prev, delta)

        ###########################
        #optional validation for posterior samples:
        if validatecheck:
            counter = 0
            while counter<=nmax and not any(element in 'v' for element in self.validate(np.transpose(testprev), feas_tol=1e-6, bounds_tol=1e-6)):#first sample: #input have to be netsamples and in form samples x reactions#, feas_tol=1e-6, bounds_tol=1e-6)
                if counter==nmax:
                    print('Tried to find valid sample', nmax, 'times without success')
                    sys.exit()
                print('searching new valid sample as validate output=', self.validate(np.transpose(testprev)))#, feas_tol=1e-6, bounds_tol=1e-6))
                pi = np.random.randint(self.n_warmup)
                delta = self.warmup[pi, ] - self.center
                testprev = step(self, testprev, delta)
                counter += 1
        self.prev = testprev
        ###########################

        if self.problem.homogeneous and (self.n_samples *
                                         self.thinning % self.nproj == 0):
            self.prev = self._reproject(self.prev)
            if not lockCenter:
                self.center = self._reproject(self.center)
        if not lockCenter:
            self.center = ((self.n_samples * self.center) / (self.n_samples + 1) +
                           self.prev / (self.n_samples + 1))
        self.n_samples += 1

    def sample(self, n, fluxes=True, likelihood=None, prior=None, validatecheck=False):
        """Generate a set of samples.

        This is the basic sampling function for all hit-and-run samplers,
        extended to support prior and likelihood functions for MCMC
        Bayesian inference of metabolic fluxes.

        Parameters
        ----------
        n : int
            The number of samples that are generated at once.
        fluxes : boolean
            Whether to return fluxes or the internal solver variables. If set
            to False will return a variable for each forward and backward flux
            as well as all additional variables you might have defined in the
            model.
        likelihood : function
            A python function which will take as an argument the flux vector
            (current sample) and return a log likelihood, computed by comparing
            the current flux vector to experimental data. This will be used to
            accept or reject samples per the Metropolis algorithm. If this value
            is passed (e.g. not False), the ACHR center will be locked,
            and not be updated. If the default (None) is used for both
            the prior and likelihood options, normal ACHR sampling will take
            place, with updates to the center on each sample.
        prior : function
            A python function which will take as an argument the flux vector
            (current sample) and return a log prior. This will be used to
            accept or reject samples per the Metropolis algorithm. If this value
            is passed (e.g. not None), the ACHR center will be locked,
            and not be updated. If the default (None) is used for both
            the prior and likelihood options, normal ACHR sampling will take
            place, with updates to the center on each sample.
        validatecheck : boolean
            Checking if current sample is valid or not. If it is True goes back
            to previous sample and finds a new valid sample (repeated for up to nmax=10 times).

        Returns
        -------
        numpy.matrix
            Returns a matrix with `n` rows, each containing a flux sample.

        Notes
        -----
        Performance of this function linearly depends on the number
        of reactions in your model and the thinning factor.

        """
        samples = np.zeros((n, self.warmup.shape[1]))

        # store the log posterior of the previous sample
        previousPosterior = False#None
        savePrev = None#np.empty(len(self.prev))# False#None
        totalSamples = 0
        rejections = 0

        # determine if we are doing centering samples, or MCMC samples
        # with a locked center
        if prior or likelihood:
            lockCenter = True
        else:
            lockCenter = False

        for i in range(1, self.thinning * n + 1):

            self.__single_iteration(lockCenter=lockCenter, validatecheck=validatecheck)

            totalSamples += 1
            if lockCenter:
                if likelihood:
                    newLikelihood = likelihood(self.prev)
                else:
                    newLikelihood = 0
                if prior:
                    newPrior = prior(self.prev)
                else:
                    newPrior = 0
                newPosterior = newLikelihood + newPrior
                acceptProbability = newPosterior - previousPosterior
                if not previousPosterior:
                    # always accept on first iteration
                    previousPosterior = newPosterior
                    savePrev = self.prev
                elif np.log(np.random.rand()) < acceptProbability:
                    # then accept if probability is high enough
                    previousPosterior = newPosterior
                    savePrev = self.prev
                    if not self.bestSample:
                        self.bestSample = (newPosterior, self.prev)
                    else:
                        if newPosterior > self.bestSample[0]:
                            self.bestSample = (newPosterior, self.prev)
                else:
                    # reject (keep previous state)
                    self.prev = savePrev
                    rejections += 1

            if i % self.thinning == 0:
                samples[i // self.thinning - 1, :] = self.prev

        print('acceptance rate: ' + str(float(totalSamples - rejections) / float(totalSamples)))

        if fluxes:
            names = [r.id for r in self.model.reactions]

            return pandas.DataFrame(
                samples[:, self.fwd_idx] - samples[:, self.rev_idx], columns=names,
            )
        else:
            names = [v.name for v in self.model.variables]

            return pandas.DataFrame(samples, columns=names)
