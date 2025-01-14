#!/usr/bin/python

# vim: set expandtab ts=4 sw=4:

"""
Implementations of the sift algorithm for Empirical Mode Decomposition.

Main Routines:
  sift                    - The classic sift algorithm
  ensemble_sift           - Noise-assisted sift algorithm
  complete_ensemble_sift  - Adapeted noise-assisted sift algorithm
  mask_sift               - Sift with masks to separate very sparse or nonlinear components
  sift_second_layer       - Apply sift to amplitude envlope of a set of IMFs

Sift Helper Routines:
  get_next_imf
  get_next_imf_mask
  get_mask_freqs
  energy_difference
  energy_stop
  sd_stop
  rilling_stop
  fixed_stop
  get_padded_extrema
  compute_parabolic_extrema
  interp_envelope
  zero_crossing_count
  is_imf

Sift Config:
  get_config
  SiftConfig


"""

import collections
import functools
import inspect
import logging
import multiprocessing as mp
import sys

import numpy as np
import yaml
from scipy import interpolate as interp
from scipy import signal

from . import spectra
from .logger import sift_logger, wrap_verbose
from .support import EMDSiftCovergeError, ensure_1d_with_singleton, ensure_2d

# Housekeeping for logging
logger = logging.getLogger(__name__)


##################################################################
# Basic SIFT

# Utilities

def get_next_imf(X, env_step_size=1, max_iters=1000, energy_thresh=None,
                 stop_method='sd', sd_thresh=.1, rilling_thresh=(0.05, 0.5, 0.05),
                 envelope_opts=None, extrema_opts=None):
    """Compute the next IMF from a data set.

    This is a helper function used within the more general sifting functions.

    Parameters
    ----------
    X : ndarray [nsamples x 1]
        1D input array containing the time-series data to be decomposed
    env_step_size : float
        Scaling of envelope prior to removal at each iteration of sift. The
        average of the upper and lower envelope is muliplied by this value
        before being subtracted from the data. Values should be between
        0 > x >= 1 (Default value = 1)
    max_iters : int > 0
        Maximum number of iterations to compute before throwing an error
    energy_thresh : float > 0
        Threshold for energy difference (in decibels) between IMF and residual
        to suggest stopping overall sift. (Default is None, recommended value is 50)
    stop_method : {'sd','rilling','fixed'}
        Flag indicating which metric to use to stop sifting and return an IMF.
    sd_thresh : float
        Used if 'stop_method' is 'sd'. The threshold at which the sift of each
        IMF will be stopped. (Default value = .1)
    rilling_thresh : tuple
        Used if 'stop_method' is 'rilling', needs to contain three values (sd1, sd2, alpha).
        An evaluation function (E) is defined by dividing the residual by the
        mode amplitude. The sift continues until E < sd1 for the fraction
        (1-alpha) of the data, and E < sd2 for the remainder.
        See section 3.2 of http://perso.ens-lyon.fr/patrick.flandrin/NSIP03.pdf

    Returns
    -------
    proto_imf : ndarray
        1D vector containing the next IMF extracted from X
    continue_flag : bool
        Boolean indicating whether the sift can be continued beyond this IMF

    Other Parameters
    ----------------
    envelope_opts : dict
        Optional dictionary of keyword arguments to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema

    See Also
    --------
    emd.sift.sift
    emd.sift.interp_envelope

    """
    X = ensure_1d_with_singleton([X], ['X'], 'get_next_imf')

    if envelope_opts is None:
        envelope_opts = {}

    proto_imf = X.copy()

    continue_imf = True
    continue_flag = True
    niters = 0
    while continue_imf:

        if stop_method != 'fixed':
            if niters == 3*max_iters//4:
                logger.debug('Sift reached {0} iterations, taking a long time to coverge'.format(niters))
            elif niters > max_iters:
                msg = 'Sift failed. No covergence after {0} iterations'.format(niters)
                raise EMDSiftCovergeError(msg)
        niters += 1

        upper = interp_envelope(proto_imf, mode='upper',
                                **envelope_opts, extrema_opts=extrema_opts)
        lower = interp_envelope(proto_imf, mode='lower',
                                **envelope_opts, extrema_opts=extrema_opts)

        # If upper or lower are None we should stop sifting altogether
        if upper is None or lower is None:
            continue_flag = False
            continue_imf = False
            logger.debug('Finishing sift: IMF has no extrema')
            continue

        # Find local mean
        avg = np.mean([upper, lower], axis=0)[:, None]

        # Remove local mean estimate from proto imf
        x1 = proto_imf - avg

        # Stop sifting if we pass threshold
        if stop_method == 'sd':
            stop, _ = sd_stop(proto_imf, x1, sd=sd_thresh, niters=niters)
        elif stop_method == 'rilling':
            stop, _ = rilling_stop(upper, lower, niters=niters,
                                   sd1=rilling_thresh[0],
                                   sd2=rilling_thresh[1],
                                   tol=rilling_thresh[2])
        elif stop_method == 'fixed':
            stop = fixed_stop(niters, max_iters)

        if stop:
            proto_imf = x1.copy()
            continue_imf = False
            continue

        proto_imf = proto_imf - (env_step_size*avg)

    if proto_imf.ndim == 1:
        proto_imf = proto_imf[:, None]

    if energy_thresh is not None:
        energy_db = _energy_difference(X, X-proto_imf)
        if energy_db > energy_thresh:
            continue_flag = False
            logger.debug('Finishing sift: energy ratio is {0}'.format(energy_db))

    return proto_imf, continue_flag


def _energy_difference(imf, residue):
    """Compute energy change in IMF during a sift.

    Parameters
    ----------
    imf : ndarray
        IMF to be evaluated
    residue : ndarray
        Remaining signal after IMF removal

    Returns
    -------
    float
        Energy difference in decibels

    Notes
    -----
    This function is used during emd.sift.energy_stop to implement the
    energy-difference sift-stopping method defined in section 3.2.4 of
    https://doi.org/10.1016/j.ymssp.2007.11.028

    """
    sumsqr = np.sum(imf**2)
    imf_energy = 20 * np.log10(sumsqr, where=sumsqr > 0)
    sumsqr = np.sum(residue ** 2)
    resid_energy = 20 * np.log10(sumsqr, where=sumsqr > 0)
    return imf_energy-resid_energy


def energy_stop(imf, residue, thresh=50, niters=None):
    """Compute energy change in IMF during a sift.

    The energy in the IMFs are compared to the energy at the start of sifting.
    The sift terminates once this ratio reaches a predefined threshold.

    Parameters
    ----------
    imf : ndarray
        IMF to be evaluated
    residue : ndarray
        Remaining signal after IMF removal
    thresh : float
        Energy ratio threshold (default=50)
    niters : int
        Number of sift iterations currently completed

    Returns
    -------
    bool
        A flag indicating whether to stop siftingg
    float
        Energy difference in decibels

    Notes
    -----
    This function implements the energy-difference sift-stopping method defined
    in section 3.2.4 of https://doi.org/10.1016/j.ymssp.2007.11.028

    """
    diff = _energy_difference(imf, residue)
    stop = bool(diff > thresh)

    if stop:
        logger.debug('Sift stopped by Energy Ratio in {0} iters with difference of {1}dB'.format(niters, diff))

    return stop, diff


def sd_stop(proto_imf, prev_imf, sd=0.2, niters=None):
    """Compute the sd sift stopping metric.

    Parameters
    ----------
    proto_imf : ndarray
        A signal which may be an IMF
    prev_imf : ndarray
        The previously identified IMF
    sd : float
        The stopping threshold
    niters : int
        Number of sift iterations currently completed
    niters : int
        Number of sift iterations currently completed

    Returns
    -------
    bool
        A flag indicating whether to stop siftingg
    float
        The SD metric value

    """
    metric = np.sum((proto_imf - prev_imf)**2) / np.sum(proto_imf**2)

    stop = metric < sd

    if stop:
        logger.debug('Sift stopped by SD-thresh in {0} iters with sd {1}'.format(niters, metric))

    return stop, metric


def rilling_stop(upper_env, lower_env, sd1=0.05, sd2=0.5, tol=0.05, niters=None):
    """Compute the Rilling et al 2003 sift stopping metric.

    This metric tries to guarantee globally small fluctuations in the IMF mean
    while taking into account locally large excursions that may occur in noisy
    signals.

    Parameters
    ----------
    upper_env : ndarray
        The upper envelope of a proto-IMF
    lower_env : ndarray
        The lower envelope of a proto-IMF
    sd1 : float
        The maximum threshold for globally small differences from zero-mean
    sd2 : float
        The maximum threshold for locally large differences from zero-mean
    tol : float (0 < tol < 1)
        (1-tol) defines the proportion of time which may contain large deviations
        from zero-mean
    niters : int
        Number of sift iterations currently completed

    Returns
    -------
    bool
        A flag indicating whether to stop siftingg
    float
        The SD metric value

    Notes
    -----
    This method is described in section 3.2 of:
    Rilling, G., Flandrin, P., & Goncalves, P. (2003, June). On empirical mode
    decomposition and its algorithms. In IEEE-EURASIP workshop on nonlinear
    signal and image processing (Vol. 3, No. 3, pp. 8-11). NSIP-03, Grado (I).
    http://perso.ens-lyon.fr/patrick.flandrin/NSIP03.pdf

    """
    avg_env = (upper_env+lower_env)/2
    amp = np.abs(upper_env-lower_env)/2

    eval_metric = np.abs(avg_env)/amp

    metric = np.mean(eval_metric > sd1)
    continue1 = metric > tol
    continue2 = np.any(eval_metric > sd2)

    stop = (continue1 or continue2) == False  # noqa: E712

    if stop:
        logger.debug('Sift stopped by Rilling-metric in {0} iters (val={1})'.format(niters, metric))

    return stop, metric


def fixed_stop(niters, max_iters):
    """Compute the fixed-iteraiton sift stopping metric.

    Parameters
    ----------
    niters : int
        Number of sift iterations currently completed
    max_iters : int
        Maximum number of sift iterations to be completed

    Returns
    -------
    bool
        A flag indicating whether to stop siftingg

    """
    stop = bool(niters == max_iters)

    if stop:
        logger.debug('Sift stopped at fixed number of {0} iterations'.format(niters))

    return stop


def _nsamples_warn(N, max_imfs):
    if max_imfs is None:
        return
    if N < 2**(max_imfs+1):
        msg = 'Inputs samples ({0}) is small for specified max_imfs ({1})'
        msg += ' very likely that {2} or fewer imfs are returned'
        logger.warning(msg.format(N, max_imfs, np.floor(np.log2(N)).astype(int)-1))


# SIFT implementation

@wrap_verbose
@sift_logger('sift')
def sift(X, sift_thresh=1e-8, max_imfs=None, verbose=None,
         imf_opts=None, envelope_opts=None, extrema_opts=None):
    """Compute Intrinsic Mode Functions from an input data vector.

    This function implements the original sift algorithm [1]_.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    sift_thresh : float
         The threshold at which the overall sifting process will stop. (Default value = 1e-8)
    max_imfs : int
         The maximum number of IMFs to compute. (Default value = None)

    Returns
    -------
    imf: ndarray
        2D array [samples x nimfs] containing he Intrisic Mode Functions from the decomposition of X.

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_next_imf
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema
    verbose : {None,'CRITICAL','WARNING','INFO','DEBUG'}
        Option to override the EMD logger level for a call to this function.

    See Also
    --------
    emd.sift.get_next_imf
    emd.sift.get_config

    Notes
    -----
    The classic sift is computed by passing an input vector with all options
    left to default

    >>> imf = emd.sift.sift(x)

    The sift can be customised by passing additional options, here we only
    compute the first four IMFs.

    >>> imf = emd.sift.sift(x, max_imfs=4)

    More detailed options are passed as dictionaries which are passed to the
    relevant lower-level functions. For instance `imf_opts` are passed to
    `get_next_imf`.

    >>> imf_opts = {'env_step_size': 1/3, 'stop_method': 'rilling'}
    >>> imf = emd.sift.sift(x, max_imfs=4, imf_opts=imf_opts)

    A modified dictionary of all options can be created using `get_config`.
    This can be modified and used by unpacking the options into a `sift` call.

    >>> conf = emd.sift.get_config('sift')
    >>> conf['max_imfs'] = 4
    >>> conf['imf_opts'] = imf_opts
    >>> imfs = emd.sift.sift(x, **conf)

    References
    ----------
    .. [1] Huang, N. E., Shen, Z., Long, S. R., Wu, M. C., Shih, H. H., Zheng,
       Q., … Liu, H. H. (1998). The empirical mode decomposition and the Hilbert
       spectrum for nonlinear and non-stationary time series analysis. Proceedings
       of the Royal Society of London. Series A: Mathematical, Physical and
       Engineering Sciences, 454(1971), 903–995.
       https://doi.org/10.1098/rspa.1998.0193

    """
    if not imf_opts:
        imf_opts = {'env_step_size': 1,
                    'sd_thresh': .1}

    X = ensure_1d_with_singleton([X], ['X'], 'sift')

    _nsamples_warn(X.shape[0], max_imfs)

    continue_sift = True
    layer = 0

    proto_imf = X.copy()

    while continue_sift:

        next_imf, continue_sift = get_next_imf(proto_imf,
                                               envelope_opts=envelope_opts,
                                               extrema_opts=extrema_opts,
                                               **imf_opts)

        if layer == 0:
            imf = next_imf
        else:
            imf = np.concatenate((imf, next_imf), axis=1)

        proto_imf = X - imf.sum(axis=1)[:, None]
        layer += 1

        if max_imfs is not None and layer == max_imfs:
            logger.info('Finishing sift: reached max number of imfs ({0})'.format(layer))
            continue_sift = False

        if np.abs(next_imf).sum() < sift_thresh:
            logger.info('Finishing sift: reached threshold {0}'.format(np.abs(next_imf).sum()))
            continue_sift = False

    return imf


##################################################################
# Ensemble SIFT variants

# Utilities

def _sift_with_noise(X, noise_scaling=None, noise=None, noise_mode='single',
                     sift_thresh=1e-8, max_imfs=None, job_ind=1,
                     imf_opts=None, envelope_opts=None, extrema_opts=None,
                     seed=None):
    """Apply white noise to a signal prior to computing a sift.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    noise_scaling : float
         Standard deviation of noise to add to each ensemble (Default value =
         None)
    noise : ndarray
         array of noise values the same size as X to add prior to sift (Default value = None)
    noise_mode : {'single','flip'}
         Flag indicating whether to compute each ensemble with noise once or
         twice with the noise and sign-flipped noise (Default value = 'single')
    sift_thresh : float
         The threshold at which the overall sifting process will stop. (Default value = 1e-8)
    max_imfs : int
         The maximum number of IMFs to compute. (Default value = None)
    job_ind : 1
        Optional job index value for display in logger (Default value = 1)
    seed : None or int
        seed for the random generator generating the noise

    Returns
    -------
    imf: ndarray
        2D array [samples x nimfs] containing he Intrisic Mode Functions from the decomposition of X.

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of arguments to be passed to emd.get_next_imf
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema

    See Also
    --------
    emd.sift.ensemble_sift
    emd.sift.complete_ensemble_sift
    emd.sift.get_next_imf

    """
    if job_ind is not None:
        p = mp.current_process()
        logger.info('Starting SIFT Ensemble: {0} on process {1}'.format(job_ind, p._identity[0]))

    # if noise is None:
    #     noise = np.random.randn(*X.shape)

    # ADDED
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(size=X.shape)

    if noise_scaling is not None:
        noise = noise * noise_scaling

    ensX = X.copy() + noise
    imf = sift(ensX, sift_thresh=sift_thresh, max_imfs=max_imfs,
               imf_opts=imf_opts, envelope_opts=envelope_opts, extrema_opts=extrema_opts)

    if noise_mode == 'single':
        return imf
    elif noise_mode == 'flip':
        ensX = X.copy() - noise
        imf += sift(ensX, sift_thresh=sift_thresh, max_imfs=max_imfs,
                    imf_opts=imf_opts, envelope_opts=envelope_opts, extrema_opts=extrema_opts)
        return imf / 2


# Implementation

@wrap_verbose
@sift_logger('ensemble_sift')
def ensemble_sift(X, nensembles=4, ensemble_noise=.2, noise_mode='single',
                  nprocesses=1, sift_thresh=1e-8, max_imfs=None, verbose=None,
                  imf_opts=None, envelope_opts=None, extrema_opts=None,
                  seed=None):
    """Compute Intrinsic Mode Functions with the ensemble EMD.

    This function implements the ensemble empirical model decomposition
    algorithm defined in [1]_. This approach sifts an ensemble of signals with
    white-noise added and treats the mean IMFs as the result. The resulting
    IMFs from the ensemble sift resembles a dyadic filter [2]_.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    nensembles : int
        Integer number of different ensembles to compute the sift across.
    ensemble_noise : float
         Standard deviation of noise to add to each ensemble (Default value = .2)
    noise_mode : {'single','flip'}
         Flag indicating whether to compute each ensemble with noise once or
         twice with the noise and sign-flipped noise (Default value = 'single')
    nprocesses : int
         Integer number of parallel processes to compute. Each process computes
         a single realisation of the total ensemble (Default value = 1)
    sift_thresh : float
         The threshold at which the overall sifting process will stop. (Default value = 1e-8)
    max_imfs : int
         The maximum number of IMFs to compute. (Default value = None)
    seed: None or int
        seed for generating the seed generator for the ensembles

    Returns
    -------
    imf : ndarray
        2D array [samples x nimfs] containing he Intrisic Mode Functions from the decomposition of X.

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_next_imf.
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema
    verbose : {None,'CRITICAL','WARNING','INFO','DEBUG'}
        Option to override the EMD logger level for a call to this function.

    See Also
    --------
    emd.sift.get_next_imf

    References
    ----------
    .. [1] Wu, Z., & Huang, N. E. (2009). Ensemble Empirical Mode Decomposition:
       A Noise-Assisted Data Analysis Method. Advances in Adaptive Data Analysis,
       1(1), 1–41. https://doi.org/10.1142/s1793536909000047
    .. [2] Wu, Z., & Huang, N. E. (2004). A study of the characteristics of
       white noise using the empirical mode decomposition method. Proceedings of
       the Royal Society of London. Series A: Mathematical, Physical and
       Engineering Sciences, 460(2046), 1597–1611.
       https://doi.org/10.1098/rspa.2003.1221


    """
    if noise_mode not in ['single', 'flip']:
        raise ValueError(
            'noise_mode: {0} not recognised, please use \'single\' or \'flip\''.format(noise_mode))

    X = ensure_1d_with_singleton([X], ['X'], 'sift')

    _nsamples_warn(X.shape[0], max_imfs)

    # Noise is defined with respect to variance in the data
    noise_scaling = X.std() * ensemble_noise

    p = mp.Pool(processes=nprocesses)

    sq = np.random.SeedSequence(seed)
    seedgen = sq.generate_state(nensembles)

    noise = None
    args = [(X, noise_scaling, noise, noise_mode, sift_thresh, max_imfs, ii, imf_opts, envelope_opts, extrema_opts,
            seedgen[ii]
            )
            for ii in range(nensembles)]

    res = p.starmap(_sift_with_noise, args)

    p.close()

    if max_imfs is None:
        max_imfs = res[0].shape[1]

    # Keep largest group of ensembles with matching number of imfs.
    nimfs = [r.shape[1] for r in res]
    uni, unic = np.unique(nimfs, return_counts=True)
    target_imfs = uni[np.argmax(unic)]
    logger.info('Retaining {0} ensembles ({1}%) each with {2} IMFs'.format(np.max(unic),
                                                                           100*(np.max(unic)/nensembles),
                                                                           target_imfs))

    imfs = np.zeros((X.shape[0], max_imfs))
    for ii in range(max_imfs):
        imfs[:, ii] = np.array([r[:, ii] for r in res if r.shape[1] == target_imfs]).mean(axis=0)

    return imfs


@wrap_verbose
@sift_logger('complete_ensemble_sift')
def complete_ensemble_sift(X, nensembles=4, ensemble_noise=.2,
                           noise_mode='single', nprocesses=1,
                           sift_thresh=1e-8, max_imfs=None, verbose=None,
                           imf_opts=None, envelope_opts=None, extrema_opts=None,
                           seed=None):
    """Compute Intrinsic Mode Functions with complete ensemble EMD.

    This function implements the complete ensemble empirical model
    decomposition algorithm defined in [1]_. This approach sifts an ensemble of
    signals with white-noise added taking a single IMF across all ensembles at
    before moving to the next IMF.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    nensembles : int
        Integer number of different ensembles to compute the sift across.
    ensemble_noise : float
         Standard deviation of noise to add to each ensemble (Default value = .2)
    noise_mode : {'single','flip'}
         Flag indicating whether to compute each ensemble with noise once or
         twice with the noise and sign-flipped noise (Default value = 'single')
    nprocesses : int
         Integer number of parallel processes to compute. Each process computes
         a single realisation of the total ensemble (Default value = 1)
    sift_thresh : float
         The threshold at which the overall sifting process will stop. (Default value = 1e-8)
    max_imfs : int
         The maximum number of IMFs to compute. (Default value = None)

    Returns
    -------
    imf: ndarray
        2D array [samples x nimfs] containing he Intrisic Mode Functions from the decomposition of X.
    noise: array_like
        The Intrisic Mode Functions from the decomposition of X.

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_next_imf.
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema
    verbose : {None,'CRITICAL','WARNING','INFO','DEBUG'}
        Option to override the EMD logger level for a call to this function.

    See Also
    --------
    emd.sift.get_next_imf

    References
    ----------
    .. [1] Torres, M. E., Colominas, M. A., Schlotthauer, G., & Flandrin, P.
       (2011). A complete ensemble empirical mode decomposition with adaptive
       noise. In 2011 IEEE International Conference on Acoustics, Speech and
       Signal Processing (ICASSP). IEEE.
       https://doi.org/10.1109/icassp.2011.5947265

    """
    p = mp.Pool(processes=nprocesses)

    X = ensure_1d_with_singleton([X], ['X'], 'sift')

    _nsamples_warn(X.shape[0], max_imfs)

    # Noise is defined with respect to variance in the data
    noise_scaling = X.std() * ensemble_noise

    continue_sift = True
    layer = 0

    # Compute the noise processes - large matrix here...
    noise = np.random.random_sample((X.shape[0], nensembles)) * noise_scaling

    # Do a normal ensemble sift to obtain the first IMF
    args = [(X, noise_scaling, noise[:, ii, None], noise_mode, sift_thresh,
             1, ii, imf_opts, envelope_opts, extrema_opts)
            for ii in range(nensembles)]
    res = p.starmap(_sift_with_noise, args)
    imf = np.array([r for r in res]).mean(axis=0)

    args = [(noise[:, ii, None], sift_thresh, 1, imf_opts) for ii in range(nensembles)]
    res = p.starmap(sift, args)
    noise = noise - np.array([r[:, 0] for r in res]).T

    while continue_sift:

        proto_imf = X - imf.sum(axis=1)[:, None]

        args = [(proto_imf, None, noise[:, ii, None], noise_mode, sift_thresh,
                 1, ii, imf_opts, envelope_opts, extrema_opts)
                for ii in range(nensembles)]
        res = p.starmap(_sift_with_noise, args)
        next_imf = np.array([r for r in res]).mean(axis=0)

        imf = np.concatenate((imf, next_imf), axis=1)

        args = [(noise[:, ii, None], sift_thresh, 1, imf_opts)
                for ii in range(nensembles)]
        res = p.starmap(sift, args)
        noise = noise - np.array([r[:, 0] for r in res]).T

        pks, _ = _find_extrema(imf[:, -1])
        if len(pks) < 2:
            continue_sift = False

        if max_imfs is not None and layer == max_imfs:
            continue_sift = False

        if np.abs(next_imf).mean() < sift_thresh:
            continue_sift = False

        layer += 1

    p.close()

    return imf, noise


##################################################################
# Mask SIFT implementations

# Utilities


def get_next_imf_mask(X, z, amp, nphases=4, nprocesses=1,
                      imf_opts=None, envelope_opts=None, extrema_opts=None):
    """Compute the next IMF from a data set a mask sift.

    This is a helper function used within the more general sifting functions.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    z : float
        Mask frequency as a proportion of the sampling rate, values between 0->z->.5
    amp : float
        Mask amplitude
    nphases : int > 0
        The number of separate sinusoidal masks to apply for each IMF, the
        phase of masks are uniformly spread across a 0<=p<2pi range
        (Default=4).
    nprocesses : int
         Integer number of parallel processes to compute. Each process computes
         an IMF from the signal plus a mask. nprocesses should be less than or
         equal to nphases, no additional benefit from setting nprocesses > nphases
         (Default value = 1)

    Returns
    -------
    proto_imf : ndarray
        1D vector containing the next IMF extracted from X
    continue_sift : bool
        Boolean indicating whether the sift can be continued beyond this IMF

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of keyword arguments to be passed to emd.get_next_imf
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema

    See Also
    --------
    emd.sift.mask_sift
    emd.sift.get_next_imf

    """
    X = ensure_1d_with_singleton([X], ['X'], 'get_next_imf_mask')

    if imf_opts is None:
        imf_opts = {}

    logger.info("Defining masks with freq {0} and amp {1} at {2} phases".format(z, amp, nphases))

    # Create normalised freq
    zf = z * 2 * np.pi
    # Create time matrix including mask phase-shifts
    t = np.repeat(np.arange(X.shape[0])[:, np.newaxis], nphases, axis=1)
    phases = np.linspace(0, (2*np.pi), nphases+1)[:nphases]
    # Create masks
    m = amp * np.cos(zf * t + phases)

    # Work with a partial function to make the parallel loop cleaner
    # This partial function contains all the settings which will be constant across jobs.
    my_get_next_imf = functools.partial(get_next_imf, **imf_opts,
                                        envelope_opts=envelope_opts,
                                        extrema_opts=extrema_opts)

    args = [[X+m[:, ii, np.newaxis]] for ii in range(nphases)]

    with mp.Pool(processes=nprocesses) as p:
        res = p.starmap(my_get_next_imf, args)

    # Collate results
    imfs = [r[0] for r in res]
    continue_flags = [r[1] for r in res]

    # star map should preserve the order of outputs so we can remove masks easily
    imfs = np.concatenate(imfs, axis=1) - m

    return imfs.mean(axis=1)[:, np.newaxis], np.any(continue_flags)


def get_mask_freqs(X, first_mask_mode='zc', imf_opts=None):
    """Determine mask frequencies for a sift.

    Parameters
    ----------
    X : ndarray
        Vector time-series
    first_mask_mode : (str, float<0.5)
        Either a string denoting a method {'zc', 'if'} or a float determining
        and initial frequency. See notes for more details.
    imf_opts : dict
        Options to be passed to get_next_imf if first_mask_mode is 'zc' or 'if'.

    Returns
    -------
    float
        Frequency for the first mask in normalised units.

    """
    if imf_opts is None:
        imf_opts = {}

    if first_mask_mode in ('zc', 'if'):
        logger.info('Computing first mask frequency with method {0}'.format(first_mask_mode))
        logger.info('Getting first IMF with no mask')
        # First IMF is computed normally
        imf, _ = get_next_imf(X, **imf_opts)

    # Compute first mask frequency from first IMF
    if first_mask_mode == 'zc':
        num_zero_crossings = zero_crossing_count(imf)[0, 0]
        z = num_zero_crossings / imf.shape[0] / 4
        logger.info('Found first mask frequency of {0}'.format(z))
    elif first_mask_mode == 'if':
        _, IF, IA = spectra.frequency_transform(imf[:, 0, None], 1, 'nht',
                                                smooth_phase=3)
        z = np.average(IF, weights=IA)
        logger.info('Found first mask frequency of {0}'.format(z))
    elif first_mask_mode < .5:
        if first_mask_mode <= 0 or first_mask_mode >= .5:
            raise ValueError("The frequency of the first mask must be 0<x<.5")
        logger.info('Using specified first mask frequency of {0}'.format(first_mask_mode))
        z = first_mask_mode

    return z


# Implementation

@wrap_verbose
@sift_logger('mask_sift')
def mask_sift(X, mask_amp=1, mask_amp_mode='ratio_imf', mask_freqs='zc',
              mask_step_factor=2, ret_mask_freq=False, max_imfs=9, sift_thresh=1e-8,
              nphases=4, nprocesses=1, verbose=None,
              imf_opts=None, envelope_opts=None, extrema_opts=None):
    """Compute Intrinsic Mode Functions using a mask sift.

    This function implements a masked sift from a dataset using a set of
    masking signals to reduce mixing of components between modes [1]_, multiple
    masks of different phases can be applied when isolating each IMF [2]_.

    This function can either compute the mask frequencies based on the fastest
    dynamics in the data (the properties of the first IMF from a standard sift)
    or apply a pre-specified set of masks.

    Parameters
    ----------
    X : ndarray
        1D input array containing the time-series data to be decomposed
    mask_amp : float or array_like
        Amplitude of mask signals as specified by mask_amp_mode. If float the
        same value is applied to all IMFs, if an array is passed each value is
        applied to each IMF in turn (Default value = 1)
    mask_amp_mode : {'abs','ratio_imf','ratio_sig'}
        Method for computing mask amplitude. Either in absolute units ('abs'),
        or as a ratio of the standard deviation of the input signal
        ('ratio_sig') or previous imf ('ratio_imf') (Default value = 'ratio_imf')
    mask_freqs : {'zc','if',float,,array_like}
        Define the set of mask frequencies to use. If 'zc' or 'if' are passed,
        the frequency of the first mask is taken from either the zero-crossings
        or instantaneous frequnecy the first IMF of a standard sift on the
        data. If a float is passed this is taken as the first mask frequency.
        Subsequent masks are defined by the mask_step_factor. If an array_like
        vector is passed, the values in the vector will specify the mask
        frequencies.
    mask_step_factor : float
        Step in frequency between successive masks (Default value = 2)
    mask_type : {'all','sine','cosine'}
        Which type of masking signal to use. 'sine' or 'cosine' options return
        the average of a +ve and -ve flipped wave. 'all' applies four masks:
        sine and cosine with +ve and -ve sign and returns the average of all
        four.
    nphases : int > 0
        The number of separate sinusoidal masks to apply for each IMF, the
        phase of masks are uniformly spread across a 0<=p<2pi range
        (Default=4).
    ret_mask_freq : bool
         Boolean flag indicating whether mask frequencies are returned (Default value = False)
    max_imfs : int
         The maximum number of IMFs to compute. (Default value = None)
    sift_thresh : float
         The threshold at which the overall sifting process will stop. (Default value = 1e-8)

    Returns
    -------
    imf : ndarray
        2D array [samples x nimfs] containing he Intrisic Mode Functions from the decomposition of X.
    mask_freqs : ndarray
        1D array of mask frequencies, if ret_mask_freq is set to True.

    Other Parameters
    ----------------
    imf_opts : dict
        Optional dictionary of keyword arguments to be passed to emd.get_next_imf
    envelope_opts : dict
        Optional dictionary of keyword options to be passed to emd.interp_envelope
    extrema_opts : dict
        Optional dictionary of keyword options to be passed to emd.get_padded_extrema
    verbose : {None,'CRITICAL','WARNING','INFO','DEBUG'}
        Option to override the EMD logger level for a call to this function.

    Notes
    -----
    Here are some example mask_sift variants you can run:

    A mask sift in which the mask frequencies are determined with
    zero-crossings and mask amplitudes by a ratio with the amplitude of the
    previous IMF (note - this is also the default):

    >>> imf = emd.sift.mask_sift(X, mask_amp_mode='ratio_imf', mask_freqs='zc')

    A mask sift in which the first mask is set at .4 of the sampling rate and
    subsequent masks found by successive division of this mask_freq by 3:

    >>> imf = emd.sift.mask_sift(X, mask_freqs=.4, mask_step_factor=3)

    A mask sift using user specified frequencies and amplitudes:

    >>> mask_freqs = np.array([.4,.2,.1,.05,.025,0])
    >>> mask_amps = np.array([2,2,1,1,.5,.5])
    >>> imf = emd.sift.mask_sift(X, mask_freqs=mask_freqs, mask_amp=mask_amps, mask_amp_mode='abs')

    See Also
    --------
    emd.sift.get_next_imf
    emd.sift.get_next_imf_mask

    References
    ----------
    .. [1] Ryan Deering, & James F. Kaiser. (2005). The Use of a Masking Signal
       to Improve Empirical Mode Decomposition. In Proceedings. (ICASSP ’05). IEEE
       International Conference on Acoustics, Speech, and Signal Processing, 2005.
       IEEE. https://doi.org/10.1109/icassp.2005.1416051
    .. [2] Tsai, F.-F., Fan, S.-Z., Lin, Y.-S., Huang, N. E., & Yeh, J.-R.
       (2016). Investigating Power Density and the Degree of Nonlinearity in
       Intrinsic Components of Anesthesia EEG by the Hilbert-Huang Transform: An
       Example Using Ketamine and Alfentanil. PLOS ONE, 11(12), e0168108.
       https://doi.org/10.1371/journal.pone.0168108

    """
    X = ensure_1d_with_singleton([X], ['X'], 'sift')

    # if first mask is if or zc - compute first imf as normal and get freq
    if isinstance(mask_freqs, (list, tuple, np.ndarray)):
        logger.info('Using user specified masks')
        if len(mask_freqs) < max_imfs:
            max_imfs = len(mask_freqs)
            logger.info("Reducing max_imfs to {0} as len(mask_freqs) < max_imfs".format(max_imfs))
    elif mask_freqs in ['zc', 'if'] or isinstance(mask_freqs, float):
        z = get_mask_freqs(X, mask_freqs, imf_opts=imf_opts)
        mask_freqs = np.array([z/mask_step_factor**ii for ii in range(max_imfs)])

    _nsamples_warn(X.shape[0], max_imfs)

    # Initialise mask amplitudes
    if mask_amp_mode == 'ratio_imf':
        sd = X.std()  # Take ratio of input signal for first IMF
    elif mask_amp_mode == 'ratio_sig':
        sd = X.std()
    elif mask_amp_mode == 'abs':
        sd = 1

    continue_sift = True
    imf_layer = 0
    proto_imf = X.copy()
    imf = []
    while continue_sift:

        # Update mask amplitudes if needed
        if mask_amp_mode == 'ratio_imf' and imf_layer > 0:
            sd = imf[:, -1].std()

        if isinstance(mask_amp, (int, float)):
            amp = mask_amp * sd
        else:
            # Should be array_like if not a single number
            amp = mask_amp[imf_layer] * sd

        logger.info('Sifting IMF-{0}'.format(imf_layer))

        next_imf, continue_sift = get_next_imf_mask(proto_imf, mask_freqs[imf_layer], amp,
                                                    nphases=nphases,
                                                    nprocesses=nprocesses,
                                                    imf_opts=imf_opts,
                                                    envelope_opts=envelope_opts,
                                                    extrema_opts=extrema_opts)

        if imf_layer == 0:
            imf = next_imf
        else:
            imf = np.concatenate((imf, next_imf), axis=1)

        proto_imf = X - imf.sum(axis=1)[:, None]

        if max_imfs is not None and imf_layer == max_imfs-1:
            logger.info('Finishing sift: reached max number of imfs ({0})'.format(imf.shape[1]))
            continue_sift = False

        if np.abs(next_imf).sum() < sift_thresh:
            continue_sift = False

        imf_layer += 1

    if ret_mask_freq:
        return imf, mask_freqs
    else:
        return imf


##################################################################
# Second Layer SIFT


@sift_logger('second_layer')
def sift_second_layer(IA, sift_func=sift, sift_args=None):
    """Compute second layer intrinsic mode functions.

    This function implements a second-layer sift to be appliede to the
    amplitude envelopes of a set of first layer IMFs [1]_.

    Parameters
    ----------
    IA : ndarray
        Input array containing a set of first layer IMFs
    sift_func : function
        Sift function to apply
    sift_args : dict
        Dictionary of sift options to be passed into sift_func

    Returns
    -------
    imf2 : ndarray
        3D array [samples x first layer imfs x second layer imfs ] containing
        the second layer IMFs

    References
    ----------
    .. [1] Huang, N. E., Hu, K., Yang, A. C. C., Chang, H.-C., Jia, D., Liang,
       W.-K., … Wu, Z. (2016). On Holo-Hilbert spectral analysis: a full
       informational spectral representation for nonlinear and non-stationary
       data. Philosophical Transactions of the Royal Society A: Mathematical,
       Physical and Engineering Sciences, 374(2065), 20150206.
       https://doi.org/10.1098/rsta.2015.0206

    """
    IA = ensure_2d([IA], ['IA'], 'sift_second_layer')

    if (sift_args is None) or ('max_imfs' not in sift_args):
        max_imfs = IA.shape[1]
    elif 'max_imfs' in sift_args:
        max_imfs = sift_args['max_imfs']

    imf2 = np.zeros((IA.shape[0], IA.shape[1], max_imfs))

    for ii in range(max_imfs):
        tmp = sift_func(IA[:, ii], **sift_args)
        imf2[:, ii, :tmp.shape[1]] = tmp

    return imf2


@sift_logger('mask_sift_second_layer')
def mask_sift_second_layer(IA, mask_freqs, sift_args=None):
    """Compute second layer IMFs using a mask sift.

    Second layer IMFs are computed from the amplitude envelopes of a set of
    first layer IMFs [1]_.A single set of masks is applied across all IMFs with
    the highest frequency mask dropped for each successive first level IMF.

    Parameters
    ----------
    IA : ndarray
        Input array containing a set of first layer IMFs
    mask_freqs : function
        Sift function to apply
    sift_args : dict
        Dictionary of sift options to be passed into sift_func

    Returns
    -------
    imf2 : ndarray
        3D array [samples x first layer imfs x second layer imfs ] containing
        the second layer IMFs

    References
    ----------
    .. [1] Huang, N. E., Hu, K., Yang, A. C. C., Chang, H.-C., Jia, D., Liang,
       W.-K., … Wu, Z. (2016). On Holo-Hilbert spectral analysis: a full
       informational spectral representation for nonlinear and non-stationary
       data. Philosophical Transactions of the Royal Society A: Mathematical,
       Physical and Engineering Sciences, 374(2065), 20150206.
       https://doi.org/10.1098/rsta.2015.0206

    """
    IA = ensure_2d([IA], ['IA'], 'sift_second_layer')

    if (sift_args is None):
        sift_args = {'max_imfs': IA.shape[1]}
    elif ('max_imfs' not in sift_args):
        sift_args['max_imfs'] = IA.shape[1]

    imf2 = np.zeros((IA.shape[0], IA.shape[1], sift_args['max_imfs']))

    for ii in range(IA.shape[1]):
        sift_args['mask_freqs'] = mask_freqs[ii:]
        tmp = mask_sift(IA[:, ii], **sift_args)
        imf2[:, ii, :tmp.shape[1]] = tmp
    return imf2


##################################################################
# SIFT Estimation Utilities


def get_padded_extrema(X, pad_width=2, mode='peaks', parabolic_extrema=False,
                       loc_pad_opts=None, mag_pad_opts=None):
    """Identify and pad the extrema in a signal.

    This function returns a set of extrema from a signal including padded
    extrema at the edges of the signal. Padding is carried out using numpy.pad.

    Parameters
    ----------
    X : ndarray
        Input signal
    pad_width : int >= 0
        Number of additional extrema to add to the start and end
    mode : {'peaks', 'troughs', 'abs_peaks'}
        Switch between detecting peaks, troughs or peaks in the abs signal
    parabolic_extrema : bool
        Flag indicating whether extrema positions should be refined by parabolic interpolation
    loc_pad_opts : dict
        Optional dictionary of options to be passed to np.pad when padding extrema locations
    mag_pad_opts : dict
        Optional dictionary of options to be passed to np.pad when padding extrema magnitudes

    Returns
    -------
    locs : ndarray
        location of extrema in samples
    mags : ndarray
        Magnitude of each extrema

    """
    if not loc_pad_opts:  # Empty dict evaluates to False
        loc_pad_opts = {'mode': 'reflect', 'reflect_type': 'odd'}
    else:
        loc_pad_opts = loc_pad_opts.copy()  # Don't work in place...
    loc_pad_mode = loc_pad_opts.pop('mode')

    if not mag_pad_opts:  # Empty dict evaluates to False
        mag_pad_opts = {'mode': 'median', 'stat_length': 1}
    else:
        mag_pad_opts = mag_pad_opts.copy()  # Don't work in place...
    mag_pad_mode = mag_pad_opts.pop('mode')

    if X.ndim == 2:
        X = X[:, 0]

    if mode == 'peaks':
        max_locs, max_ext = _find_extrema(X, parabolic_extrema=parabolic_extrema)
    elif mode == 'troughs':
        max_locs, max_ext = _find_extrema(-X, parabolic_extrema=parabolic_extrema)
        max_ext = -max_ext
    elif mode == 'abs_peaks':
        max_locs, max_ext = _find_extrema(np.abs(X), parabolic_extrema=parabolic_extrema)
    else:
        raise ValueError('Mode {0} not recognised by get_padded_extrema'.format(mode))

    # Return nothing if we don't have enough extrema
    if (len(max_locs) == 0) or (max_locs.size <= 1):
        return None, None

    # Determine how much padding to use
    if max_locs.size < pad_width:
        pad_width = max_locs.size

    # Return now if we're not padding
    if (pad_width is None) or (pad_width == 0):
        return max_locs, max_ext

    # Pad peak locations
    ret_max_locs = np.pad(max_locs, pad_width, loc_pad_mode, **loc_pad_opts)

    # Pad peak magnitudes
    ret_max_ext = np.pad(max_ext, pad_width, mag_pad_mode, **mag_pad_opts)

    # Keep padding if the locations don't stretch to the edge
    while max(ret_max_locs) < len(X) or min(ret_max_locs) >= 0:
        ret_max_locs = np.pad(ret_max_locs, pad_width, loc_pad_mode, **loc_pad_opts)
        ret_max_ext = np.pad(ret_max_ext, pad_width, mag_pad_mode, **mag_pad_opts)

    return ret_max_locs, ret_max_ext


def _find_extrema(X, peak_prom_thresh=None, parabolic_extrema=False):
    """Identify extrema within a time-course.

    This function detects extrema using a scipy.signals.argrelextrema. Extrema
    locations can be refined by parabolic intpolation and optionally
    thresholded by peak prominence.

    Parameters
    ----------
    X : ndarray
       Input signal
    peak_prom_thresh : {None, float}
       Only include peaks which have prominences above this threshold or None
       for no threshold (default is no threshold)
    parabolic_extrema : bool
        Flag indicating whether peak estimation should be refined by parabolic
        interpolation (default is False)

    Returns
    -------
    locs : ndarray
        Location of extrema in samples
    extrema : ndarray
        Value of each extrema

    """
    ext_locs = signal.argrelextrema(X, np.greater, order=1)[0]

    if len(ext_locs) == 0:
        return np.array([]), np.array([])

    if peak_prom_thresh is not None:
        prom, _, _ = signal._peak_finding.peak_prominences(X, ext_locs, wlen=3)
        keeps = np.where(prom > peak_prom_thresh)[0]
        ext_locs = ext_locs[keeps]

    if parabolic_extrema:
        y = np.c_[X[ext_locs-1], X[ext_locs], X[ext_locs+1]].T
        ext_locs, max_pks = compute_parabolic_extrema(y, ext_locs)
        return ext_locs, max_pks
    else:
        return ext_locs, X[ext_locs]


def compute_parabolic_extrema(y, locs):
    """Compute a parabolic refinement extrema locations.

    Parabolic refinement is computed from in triplets of points based on the
    method described in section 3.2.1 from Rato 2008 [1]_.

    Parameters
    ----------
    y : array_like
        A [3 x nextrema] array containing the points immediately around the
        extrema in a time-series.
    locs : array_like
        A [nextrema] length vector containing x-axis positions of the extrema

    Returns
    -------
    numpy array
        The estimated y-axis values of the interpolated extrema
    numpy array
        The estimated x-axis values of the interpolated extrema

    References
    ----------
    .. [1] Rato, R. T., Ortigueira, M. D., & Batista, A. G. (2008). On the HHT,
    its problems, and some solutions. Mechanical Systems and Signal Processing,
    22(6), 1374–1394. https://doi.org/10.1016/j.ymssp.2007.11.028

    """
    # Parabola equation parameters for computing y from parameters a, b and c
    # w = np.array([[1, 1, 1], [4, 2, 1], [9, 3, 1]])
    # ... and its inverse for computing a, b and c from y
    w_inv = np.array([[.5, -1, .5], [-5/2, 4, -3/2], [3, -3, 1]])
    abc = w_inv.dot(y)

    # Find co-ordinates of extrema from parameters abc
    tp = - abc[1, :] / (2*abc[0, :])
    t = tp - 2 + locs
    y_hat = tp*abc[1, :]/2 + abc[2, :]

    return t, y_hat


def interp_envelope(X, mode='upper', interp_method='splrep', extrema_opts=None,
                    ret_extrema=False):
    """Interpolate the amplitude envelope of a signal.

    Parameters
    ----------
    X : ndarray
        Input signal
    mode : {'upper','lower','combined'}
         Flag to set which envelope should be computed (Default value = 'upper')
    interp_method : {'splrep','pchip','mono_pchip'}
         Flag to indicate which interpolation method should be used (Default value = 'splrep')

    Returns
    -------
    ndarray
        Interpolated amplitude envelope

    """
    if not extrema_opts:  # Empty dict evaluates to False
        extrema_opts = {'pad_width': 2,
                        'loc_pad_opts': None,
                        'mag_pad_opts': None}
    else:
        extrema_opts = extrema_opts.copy()  # Don't work in place...

    if interp_method not in ['splrep', 'mono_pchip', 'pchip']:
        raise ValueError("Invalid interp_method value")

    if mode == 'upper':
        locs, pks = get_padded_extrema(X, mode='peaks', **extrema_opts)
    elif mode == 'lower':
        locs, pks = get_padded_extrema(X, mode='troughs', **extrema_opts)
    elif mode == 'combined':
        locs, pks = get_padded_extrema(X, mode='abs_peaks', **extrema_opts)
    else:
        raise ValueError('Mode not recognised. Use mode= \'upper\'|\'lower\'|\'combined\'')

    if locs is None:
        return None

    # Run interpolation on envelope
    t = np.arange(locs[0], locs[-1])
    if interp_method == 'splrep':
        f = interp.splrep(locs, pks)
        env = interp.splev(t, f)
    elif interp_method == 'mono_pchip':
        pchip = interp.PchipInterpolator(locs, pks)
        env = pchip(t)
    elif interp_method == 'pchip':
        pchip = interp.pchip(locs, pks)
        env = pchip(t)

    t_max = np.arange(locs[0], locs[-1])
    tinds = np.logical_and((t_max >= 0), (t_max < X.shape[0]))

    env = np.array(env[tinds])

    if env.shape[0] != X.shape[0]:
        raise ValueError('Envelope length does not match input data {0} {1}'.format(
            env.shape[0], X.shape[0]))

    if ret_extrema:
        return env, (locs, pks)
    else:
        return env


def zero_crossing_count(X):
    """Count the number of zero-crossings within a time-course.

    Zero-crossings are counted through differentiation of the sign of the
    signal.

    Parameters
    ----------
    X : ndarray
        Input array

    Returns
    -------
    int
        Number of zero-crossings

    """
    if X.ndim == 2:
        X = X[:, None]

    return (np.diff(np.sign(X), axis=0) != 0).sum(axis=0)


def is_imf(imf, avg_tol=5e-2, envelope_opts=None, extrema_opts=None):
    """Determine whether a signal is a 'true IMF'.

    Two criteria are tested. Firstly, the number of extrema and number of
    zero-crossings must differ by zero or one. Secondly,the mean of the upper
    and lower envelopes must be within a tolerance of zero.

    Parameters
    ----------
    imf : 2d array
        Array of signals to check [nsamples x nimfs]
    avg_tol : float
        Tolerance of acceptance for criterion two. The sum-square of the mean
        of the upper and lower envelope must be below avg_tol of the sum-square
        of the signal being checked.
    envelope_opts : dict
        Dictionary of envelope estimation options, must be identical to options
        used when estimating IMFs.
    extrema_opts : dict
        Dictionary of extrema estimation options, must be identical to options
        used when estimating IMFs.

    Parameters
    ----------
    array [2 x nimfs]
        Boolean array indicating whether each IMF passed each test.

    Notes
    -----
    These are VERY strict criteria to apply to real data. The tests may
    indicate a fail if the sift doesn't coverge well in a short segment of the
    signal when the majority of the IMF is well behaved.

    The tests are only valid if called with identical envelope_opts and
    extrema_opts as were used in the sift estimation.

    """
    imf = ensure_2d([imf], ['imf'], 'is_imf')

    if envelope_opts is None:
        envelope_opts = {}

    checks = np.zeros((imf.shape[1], 2), dtype=bool)

    for ii in range(imf.shape[1]):

        # Extrema and zero-crossings differ by <=1
        num_zc = zero_crossing_count(imf[:, ii])
        num_ext = signal.find_peaks(imf[:, ii])[0].shape[0] + signal.find_peaks(-imf[:, ii])[0].shape[0]

        # Mean of envelopes should be zero
        upper = interp_envelope(imf[:, ii], mode='upper',
                                **envelope_opts, extrema_opts=extrema_opts)
        lower = interp_envelope(imf[:, ii], mode='lower',
                                **envelope_opts, extrema_opts=extrema_opts)

        # If upper or lower are None we should stop sifting altogether
        if upper is None or lower is None:
            logger.debug('IMF-{0} False - no peaks detected')
            continue

        # Find local mean
        avg = np.mean([upper, lower], axis=0)[:, None]
        avg_sum = np.sum(np.abs(avg))
        imf_sum = np.sum(np.abs(imf[:, ii]))
        diff = avg_sum / imf_sum

        # TODO: Could probably add a Rilling-like criterion here. ie - is_imf
        # is true if (1-alpha)% of time is within some thresh
        checks[ii, 0] = np.abs(np.diff((num_zc, num_ext))) <= 1
        checks[ii, 1] = diff < avg_tol

        msg = 'IMF-{0} {1} - {2} extrema and {3} zero-crossings. Avg of envelopes is {4:.4}/{5:.4} ({6:.4}%)'
        msg = msg.format(ii, np.alltrue(checks[ii, :]), num_ext, num_zc, avg_sum, imf_sum, 100*diff)
        logger.debug(msg)

    return checks

##################################################################
# SIFT Config Utilities


class SiftConfig(collections.abc.MutableMapping):
    """A dictionary-like object specifying keyword arguments configuring a sift."""

    def __init__(self, name='sift', *args, **kwargs):
        """Specify keyword arguments configuring a sift."""
        self.store = dict()
        self.sift_type = name
        self.update(dict(*args, **kwargs))  # use the free update to set keys

    def __getitem__(self, key):
        """Return an item from the internal store."""
        key = self.__keytransform__(key)
        if isinstance(key, list):
            if len(key) == 2:
                return self.store[key[0]][key[1]]
            elif len(key) == 3:
                return self.store[key[0]][key[1]][key[2]]
        else:
            return self.store[key]

    def __setitem__(self, key, value):
        """Set or change the value of an item in the internal store."""
        key = self.__keytransform__(key)
        if isinstance(key, list):
            if len(key) == 2:
                self.store[key[0]][key[1]] = value
            elif len(key) == 3:
                self.store[key[0]][key[1]][key[2]] = value
        else:
            self.store[key] = value

    def __delitem__(self, key):
        """Remove an item from the internal store."""
        key = self.__keytransform__(key)
        if isinstance(key, list):
            if len(key) == 2:
                del self.store[key[0]][key[1]]
            elif len(key) == 3:
                del self.store[key[0]][key[1]][key[2]]
        else:
            del self.store[key]

    def __iter__(self):
        """Iterate through items in the internal store."""
        return iter(self.store)

    def __str__(self):
        """Print summary of internal store."""
        out = []
        lower_level = ['imf_opts', 'envelope_opts', 'extrema_opts']
        for stage in self.store.keys():
            if stage not in lower_level:
                out.append('{0} : {1}'.format(stage, self.store[stage]))
            else:
                out.append(stage + ':')
                for key in self.store[stage].keys():
                    out.append('    {0} : {1}'.format(key, self.store[stage][key]))

        return '%s %s\n%s' % (self.sift_type, self.__class__, '\n'.join(out))

    def __repr__(self):
        """Print summary of internal store."""
        return "<{0} ({1})>".format(self.__module__ + '.' + type(self).__name__, self.sift_type)

    def _repr_html_(self):
        _str_html = "<h3><b>%s %s</b></h3><hr><ul>" % (self.sift_type, self.__class__)
        lower_level = ['imf_opts', 'envelope_opts', 'extrema_opts']
        for stage in self.store.keys():
            if stage not in lower_level:
                _str_html += '<li><b>{0}</b> : {1}</li>'.format(stage, self.store[stage])
            else:
                outer_list = '<li><b>{0}</b></li>%s'.format(stage)
                inner_list = '<ul>'
                for key in self.store[stage].keys():
                    inner_list += '<li><i>{0}</i> : {1}</li>'.format(key, self.store[stage][key])
                _str_html += outer_list % (inner_list + '</ul>')
        return _str_html + '</ul>'

    def __len__(self):
        """Return number of items in internal store."""
        return len(self.store)

    def __keytransform__(self, key):
        """Split a merged dictionary key into separate levels."""
        key = key.split('/')
        if len(key) == 1:
            return key[0]
        else:
            if len(key) > 3:
                raise ValueError("Requested key is nested too deep. Should be a \
                                 maximum of three levels separated by '/'")
            return key

    def _get_yamlsafe_dict(self):
        """Return copy of internal store with values prepped for saving into yaml format."""
        conf = self.store.copy()
        conf = _array_or_tuple_to_list(conf)
        return [{'sift_type': self.sift_type}, conf]

    def to_yaml_text(self):
        """Return a copy of the internal store in yaml-text format."""
        return yaml.dump(self._get_yamlsafe_dict(), sort_keys=False)

    def to_yaml_file(self, fname):
        """Save a copy of the internal store in a specified yaml file."""
        with open(fname, 'w') as f:
            yaml.dump_all(self._get_yamlsafe_dict(), f, sort_keys=False)
        logger.info("Saved SiftConfig ({0}) to {1}".format(self, fname))

    @classmethod
    def from_yaml_file(cls, fname):
        """Create and return a new SiftConfig object with options loaded from a yaml file."""
        ret = cls()
        with open(fname, 'r') as f:
            cfg = [d for d in yaml.load_all(f, Loader=yaml.FullLoader)]
            if len(cfg) == 1:
                ret.store = cfg[0]
                ret.sift_type = 'Unknown'
            else:
                ret.sift_type = cfg[0]['sift_type']
                ret.store = cfg[1]
        logger.info("Loaded SiftConfig ({0}) from {1}".format(ret, fname))

        return ret

    @classmethod
    def from_yaml_stream(cls, stream):
        """Create and return a new SiftConfig object with options loaded from a yaml stream."""
        ret = cls()
        ret.store = yaml.load(stream, Loader=yaml.FullLoader)
        return ret

    def get_func(self):
        """Get a partial-function coded with the options from this config."""
        mod = sys.modules[__name__]
        func = getattr(mod, self.sift_type)
        return functools.partial(func, **self.store)


def get_config(siftname='sift'):
    """Return a SiftConfig with default options for a specified sift variant.

    Helper function for specifying config objects specifying parameters to be
    used in a sift. The functions used during the sift areinspected
    automatically and default values are populated into a nested dictionary
    which can be modified and used as input to one of the sift functions.

    Parameters
    ----------
    siftname : str
        Name of the sift function to find configuration from

    Returns
    -------
    SiftConfig
        A modified dictionary containing the sift specification

    Notes
    -----
    The sift config acts as a nested dictionary which can be modified to
    specify parameters for different parts of the sift. This is initialised
    using this function

    >>> config = emd.sift.get_config()

    The first level of the dictionary contains three sub-dicts configuring
    different parts of the algorithm:

    >>> config['imf_opts'] # options passed to `get_next_imf`
    >>> config['envelope_opts'] # options passed to interp_envelope
    >>> config['extrema_opts'] # options passed to get_padded_extrema

    Specific values can be modified in the dictionary

    >>> config['extrema_opts']['parabolic_extrema'] = True

    or using this shorthand

    >>> config['imf_opts/env_step_factor'] = 1/3

    Finally, the SiftConfig dictionary should be nested before being passed as
    keyword arguments to a sift function.

    >>> imfs = emd.sift.sift(X, **config)

    """
    # Extrema padding opts are hard-coded for the moment, these run through
    # np.pad which has a complex signature
    mag_pad_opts = {'mode': 'median', 'stat_length': 1}
    loc_pad_opts = {'mode': 'reflect', 'reflect_type': 'odd'}

    # Get defaults for extrema detection and padding
    extrema_opts = _get_function_opts(get_padded_extrema, ignore=['X', 'mag_pad_opts',
                                                                  'loc_pad_opts',
                                                                  'mode'])

    # Get defaults for envelope interpolation
    envelope_opts = _get_function_opts(interp_envelope, ignore=['X', 'extrema_opts', 'mode', 'ret_extrema'])

    # Get defaults for computing IMFs
    imf_opts = _get_function_opts(get_next_imf, ignore=['X', 'envelope_opts', 'extrema_opts'])

    # Get defaults for the given sift variant
    sift_types = ['sift', 'ensemble_sift', 'complete_ensemble_sift',
                  'mask_sift', 'mask_sift_adaptive', 'mask_sift_specified']
    if siftname in sift_types:
        mod = sys.modules[__name__]
        sift_opts = _get_function_opts(getattr(mod, siftname), ignore=['X', 'imf_opts'
                                                                       'envelope_opts',
                                                                       'extrema_opts'])
    else:
        raise AttributeError('Sift siftname not recognised: please use one of {0}'.format(sift_types))

    out = SiftConfig(siftname)
    for key in sift_opts:
        out[key] = sift_opts[key]
    out['imf_opts'] = imf_opts
    out['envelope_opts'] = envelope_opts
    out['extrema_opts'] = extrema_opts
    out['extrema_opts/mag_pad_opts'] = mag_pad_opts
    out['extrema_opts/loc_pad_opts'] = loc_pad_opts

    return out


def _get_function_opts(func, ignore=None):
    """Inspect a function and extract its keyword arguments and their default values.

    Parameters
    ----------
    func : function
        handle for the function to be inspected
    ignore : {None or list}
        optional list of keyword argument names to be ignored in function
        signature

    Returns
    -------
    dict
        Dictionary of keyword arguments with keyword keys and default value
        values.

    """
    if ignore is None:
        ignore = []
    out = {}
    sig = inspect.signature(func)
    for p in sig.parameters:
        if p not in out.keys() and p not in ignore:
            out[p] = sig.parameters[p].default
    return out


def _array_or_tuple_to_list(conf):
    """Convert an input array or tuple to list (for yaml_safe dict creation."""
    for key, val in conf.items():
        if isinstance(val, np.ndarray):
            conf[key] = val.tolist()
        elif isinstance(val, dict):
            conf[key] = _array_or_tuple_to_list(conf[key])
        elif isinstance(val, tuple):
            conf[key] = list(val)
    return conf
