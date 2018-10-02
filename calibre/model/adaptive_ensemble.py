"""Model and Sampling functions for Adaptive Ensemble using Tail-free Prior. """

import functools

import numpy as np

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability import edward2 as ed

import calibre.util.inference as inference_util

from calibre.model import gaussian_process as gp
from calibre.model import tailfree_process as tail_free

from calibre.util.model import sparse_softmax

tfd = tfp.distributions

_NOISE_PRIOR_MEAN = -5.
_NOISE_PRIOR_SDEV = 1.


def sparse_conditional_weight(X, base_pred, temp,
                              family_tree=None,
                              kernel_func=gp.rbf,
                              link_func=sparse_softmax,
                              ridge_factor=1e-3,
                              name="ensemble_weight",
                              **kwargs):
    r"""Defines the nonparametric (tail-free process) prior for p(model, feature).

    Defines the conditional distribution of model given feature as:

        w( model | x ) = link_func( w_raw(x) )
        w_raw(x) ~ gaussian_process[0, k_w(x)]

    Notes:
        For K models, only K-1 gp priors will be created, such that the output
        weight for the first model will be 1/(1 + \sum_i exp(f_i)), and the
        weight for the rest of the models are: exp(f_i)/(1 + \sum_i exp(f_i)).

    Args:
        X: (np.ndarray) Input features of dimension (N, D)
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models. For detail, see calibre.
        temp: (ed.RandomVariable of float32) list of unnormalized
            temperature parameter for sparse ensemble (to pass to link_func).
            It's dimension must be (len(family_tree), ) (i.e. one temp parameter for each node
            in the model family tree).
        family_tree: (dict of list or None) A dictionary of list of strings to
            specify the family tree between models, if None then assume there's
            no structure (i.e. flat structure).
        kernel_func: (function) kernel function for base ensemble,
            with args (X, **kwargs).
        link_func: (function) a link function that transforms the unnormalized
            base ensemble weights to a K-dimension simplex.
            This function has args (logits, temp)
        ridge_factor: (float32) ridge factor to stabilize Cholesky decomposition.
        name: (str) name of the ensemble weight node on the computation graph.
        **kwargs: Additional parameters to pass to kernel_func.

    Returns:
        (tf.Tensor of float32) normalized ensemble weights, dimension (N, M).
    """
    # TODO(jereliu): to move to tailfree_process.
    if family_tree:
        raise NotImplementedError

    # specify un-normalized GP weights
    base_names = list(base_pred.keys())

    # Note: skip the first model
    W_raw = tf.stack([
        gp.prior(X, kernel_func=kernel_func,
                 ridge_factor=ridge_factor,
                 name='base_weight_{}'.format(base_names), **kwargs)
        for base_names in base_names], axis=1)

    # specify normalized GP weights by family group
    W_model = link_func(W_raw, tf.exp(temp), name=name)

    return W_model


""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
""" Main model definition """
""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""


def model_flat(X, base_pred, family_tree=None, ls_weight=1., ls_resid=1., **kwargs):
    r"""Defines the sparse adaptive ensemble model.

        y           ~   N(f, sigma^2)
        f(x)        ~   gaussian_process(sum{ f_model(x) * w_model(x) }, k_resid(x))
        w_model     =   tail_free_process(w0_model)
        w0_model(x) ~   gaussian_process(0, k_w(x))

    where the tail_free_process is defined by sparse_ensemble_weight.

    Args:
        X: (np.ndarray) Input features of dimension (N, D)
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models. For each item in the dictionary,
            key is the model name, and value is the model prediction with
            dimension (N, ).
        ls_weight: (float32) lengthscale for the kernel of ensemble weight GPs.
        ls_resid: (float32) lengthscale for the kernel of residual process GP.
        family_tree: (dict of list or None) A dictionary of list of strings to
            specify the family tree between models, if None then assume there's
            no structure (i.e. flat).
        **kwargs: Additional parameters to pass to sparse_ensemble_weight.

    Returns:
        (tf.Tensors of float32) model parameters.
    """
    # check dimension
    N, D = X.shape
    for key, value in base_pred.items():
        if not value.shape == (N,):
            raise ValueError(
                "All base-model predictions should have shape ({},), but"
                "observed {} for '{}'".format(N, value.shape, key))

    # specify tail-free priors for ensemble weight
    if not family_tree:
        temp = ed.Normal(loc=tail_free._TEMP_PRIOR_MEAN,
                         scale=tail_free._TEMP_PRIOR_SDEV, name='temp')
    else:
        # specify a list of temp parameters for each node in the tree
        temp = ed.Normal(loc=[tail_free._TEMP_PRIOR_MEAN] * len(family_tree),
                         scale=tail_free._TEMP_PRIOR_SDEV, name='temp')

    # specify ensemble weight
    W = sparse_conditional_weight(X, base_pred, temp,
                                  family_tree=family_tree, ls=ls_weight,
                                  name="ensemble_weight",
                                  **kwargs)

    # specify ensemble prediction
    F = np.asarray(list(base_pred.values())).T
    FW = tf.multiply(F, W)
    ensemble_mean = tf.reduce_sum(FW, axis=1, name="ensemble_mean")

    # specify residual process
    ensemble_resid = gp.prior(
        X, ls_resid, kernel_func=gp.rbf, name="ensemble_resid")

    # specify observation noise
    sigma = ed.Normal(loc=_NOISE_PRIOR_MEAN,
                      scale=_NOISE_PRIOR_SDEV, name="sigma")

    # specify observation
    y = ed.MultivariateNormalDiag(loc=ensemble_mean + ensemble_resid,
                                  scale_identity_multiplier=tf.exp(sigma),
                                  name="y")
    return y


def model_tailfree(X, base_pred, family_tree=None,
                   ls_weight=1., ls_resid=1., **kwargs):
    r"""Defines the sparse adaptive ensemble model.

        y           ~   N(f, sigma^2)
        f(x)        ~   gaussian_process(sum{ f_model(x) * w_model(x) }, k_resid(x))
        w_model     =   tail_free_process(w0_model)
        w0_model(x) ~   gaussian_process(0, k_w(x))

    where the tail_free_process is defined by sparse_ensemble_weight.

    Args:
        X: (np.ndarray) Input features of dimension (N, D)
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models. For each item in the dictionary,
            key is the model name, and value is the model prediction with
            dimension (N, ).
        ls_weight: (float32) lengthscale for the kernel of ensemble weight GPs.
        ls_resid: (float32) lengthscale for the kernel of residual process GP.
        family_tree: (dict of list or None) A dictionary of list of strings to
            specify the family tree between models, if None then assume there's
            no structure (i.e. flat).
        **kwargs: Additional parameters to pass to tail_free.prior.

    Returns:
        (tf.Tensors of float32) model parameters.
    """
    # check dimension
    N, D = X.shape
    for key, value in base_pred.items():
        if not value.shape == (N,):
            raise ValueError(
                "All base-model predictions should have shape ({},), but"
                "observed {} for '{}'".format(N, value.shape, key))

    # specify tail-free priors for ensemble weight
    ensemble_weights, model_names = tail_free.prior(X, base_pred,
                                                    family_tree=family_tree,
                                                    ls=ls_weight,
                                                    name="ensemble_weight",
                                                    **kwargs)

    # specify ensemble prediction
    base_models = np.asarray([base_pred[name] for name in model_names]).T
    FW = tf.multiply(base_models, ensemble_weights)
    ensemble_mean = tf.reduce_sum(FW, axis=1, name="ensemble_mean")

    # specify residual process
    ensemble_resid = gp.prior(
        X, ls_resid, kernel_func=gp.rbf, name="ensemble_resid")

    # specify observation noise
    sigma = ed.Normal(loc=_NOISE_PRIOR_MEAN,
                      scale=_NOISE_PRIOR_SDEV, name="sigma")

    # specify observation
    y = ed.MultivariateNormalDiag(loc=ensemble_mean + ensemble_resid,
                                  scale_identity_multiplier=tf.exp(sigma),
                                  name="y")
    return y


""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
""" Sampling functions for intermediate random variables """
""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""


def sample_posterior_weight_flat(weight_sample, temp_sample,
                                 link_func=sparse_softmax):
    """Computes posterior sample for f_ensemble functions.

    Args:
        weight_sample: (np.ndarray or list) list of un-normalized GP weights
            for sparse ensemble, each with dimension (K, N_sample, N_obs)
        temp_sample: (np.ndarray) list of un-normalized temperature parameter
            for sparse ensemble.
        link_func: (function) a link function that transforms the unnormalized
            base ensemble weights to a K-dimension simplex.
            This function has args (logits, temp)

    Returns:
        (tf.Tensor of float32) Posterior samples of f_ensemble of dimension
            (N_sample, N_obs, )
    Raises:
        ValueError: If first dimension of weight_sample does not equal to
            that of the temp_sample
    """
    N_sample, = temp_sample.shape

    if isinstance(weight_sample, list):
        weight_sample = np.asanyarray(weight_sample)
        # shape is now (N_sample, N_obs, K)
        weight_sample = np.moveaxis(weight_sample, 0, -1)
        if not weight_sample.shape[0] == N_sample:
            raise ValueError(
                "Sample size of weight_sample (dim={}) doesn't match "
                "that of the temp_sample ({})".format(
                    weight_sample.shape, N_sample))

    # compute ensemble weights
    ensemble_weight_sample = link_func(weight_sample, tf.exp(temp_sample),
                                       name="ensemble_weight")

    return ensemble_weight_sample


def sample_posterior_mean_flat(base_pred, weight_sample, temp_sample,
                               link_func=sparse_softmax):
    """Computes posterior sample for f_ensemble functions.

    Args:
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models. For each item in the dictionary,
            key is the model name, and value is the model prediction with
            dimension (N_obs, ).
        weight_sample: (np.ndarray or list) list of un-normalized GP weights
            for sparse ensemble, each with dimension (K, N_sample, N_obs)
        temp_sample: (np.ndarray) list of un-normalized temperature parameter
            for sparse ensemble.
        link_func: (function) a link function that transforms the unnormalized
            base ensemble weights to a K-dimension simplex.
            This function has args (logits, temp)

    Returns:
        (tf.Tensor of float32) Posterior samples of f_ensemble of dimension
            (N_sample, N_obs, )
    Raises:
        ValueError: If first dimension of weight_sample does not equal to
            that of the temp_sample
    """
    # compute ensemble weights
    W_sample = sample_posterior_weight_flat(
        weight_sample, temp_sample, link_func=link_func)

    # compute ensemble function
    F = np.asarray(list(base_pred.values())).T  # (N_obs, K)
    FW_sample = tf.multiply(F, W_sample)
    f_ens_sample = tf.reduce_sum(FW_sample, axis=2, name="f_ensemble")

    return f_ens_sample


def sample_posterior_tailfree(X, base_pred_dict, family_tree,
                              weight_gp_dict, temp_dict, resid_gp_sample,
                              kernel_func=gp.rbf, ls_weight=1.,
                              link_func=sparse_softmax,
                              ridge_factor=1e-3):
    """Obtain Samples from the posterior mean and posterior predictive.

    Args:
        X: (np.ndarray) Input features of dimension (N, D) corresponding to base_pred_dict.
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models.
        family_tree: (dict of list or None) A dictionary of list of strings to
            specify the family tree between models, if None then assume there's
            no structure (i.e. flat structure).
        weight_gp_dict: (dict of np.ndarray) Dictionary of samples of raw weights
            for each non-root model/model family.
        temp_dict: (dict of np.ndarray) Dictionary of temperature random variables
            for each parent model.
        resid_gp_sample: (np.ndarray) GP samples for residual process corresponding to X.
        kernel_func: (function) kernel function for base ensemble,
            with args (X, **kwargs).
        ls_weight: (float32) lengthscale for the kernel of ensemble weight GPs.
        link_func: (function) a link function that transforms the unnormalized
            base ensemble weights to a K-dimension simplex.
        ridge_factor: (float32) ridge factor to stabilize Cholesky decomposition.

    Returns:
        ensemble_sample: (np.ndarray) Samples from full posterior predictive.
        ensemble_mean: (np.ndarray) Samples from posterior mean.
        ensemble_weights: (np.ndarray) Samples of leaf model weights.
        cond_weights_dict: (dict of np.ndarray) Dictionary of conditional weights
            for each non-root node.
        ensemble_model_names: (list of str) Names of the leaf models corresponding to
            ensemble_weights.
    """
    eval_graph = tf.Graph()
    with eval_graph.as_default():
        cond_weight_tensors_dict = (
            tail_free.compute_cond_weights(X,
                                           family_tree=family_tree,
                                           raw_weights_dict=weight_gp_dict,
                                           parent_temp_dict=temp_dict,
                                           kernel_func=kernel_func,
                                           link_func=link_func,
                                           ridge_factor=ridge_factor,
                                           ls=ls_weight))

        ensemble_weight_tensors, ensemble_model_names = (
            tail_free.compute_leaf_weights(node_weights=cond_weight_tensors_dict,
                                           family_tree=family_tree,
                                           name='ensemble_weight')
        )

        base_model_pred = np.asarray(
            [base_pred_dict[model_name] for model_name in ensemble_model_names]).T

        FW = tf.multiply(base_model_pred, ensemble_weight_tensors)
        ensemble_mean_tensor = tf.reduce_sum(FW, axis=-1, name="ensemble_mean")

        eval_graph.finalize()

    with tf.Session(graph=eval_graph) as sess:
        (ensemble_mean,
         ensemble_weights,
         cond_weights_dict) = sess.run([ensemble_mean_tensor,
                                        ensemble_weight_tensors,
                                        cond_weight_tensors_dict])

        # compute sample for full posterior
        ensemble_sample = ensemble_mean + resid_gp_sample

    return (ensemble_sample, ensemble_mean,
            ensemble_weights, cond_weights_dict, ensemble_model_names)


""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
""" Variational Family """
""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""


def variational_family(X, base_pred, family_tree=None,
                       gp_vi_family=gp.variational_mfvi,
                       ls_weight=1., ls_resid=1., **kwargs):
    """Defines the variational family for sparse adaptive ensemble model.

    Args:
        X: (np.ndarray) Input features of dimension (N, D)
        base_pred: (dict of np.ndarray) A dictionary of out-of-sample prediction
            from base models. For each item in the dictionary,
            key is the model name, and value is the model prediction with
            dimension (N, ).
        gp_vi_family: (function) A variational family for node weight
            GPs in the family tree.
        ls_weight: (float32) lengthscale for the kernel of ensemble weight GPs.
        ls_resid: (float32) lengthscale for the kernel of residual process GP.
        family_tree: (dict of list or None) A dictionary of list of strings to
            specify the family tree between models, if None then assume there's
            no structure (i.e. flat).
        **kwargs: Additional parameters to pass to tail_free/gp.variational_family.

    Returns:
        Collections of variational random variables/parameters:

        > Random variables

        temp_dict: (dict of ed.RandomVariable) Dictionary of temperature RVs
            for each parent model.
        weight_f_dict: (dict of ed.RandomVariable) Dictionary of GP random variables
            for each non-root model/model family.
        resid_f: (ed.RandomVariable) GP random variable for residual process.
        sigma: (ed.RandomVariable) normal RV for log standard derivation of
            observation noise.

        > GP variational parameters:

        weight_gp_mean_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the mean of node weight GP.
        weight_gp_vcov_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the stddev or covariance matrix of node weight GP.
        resid_gp_mean_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the mean of residual GP.
        resid_gp_vcov_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the stddev or covariance matrix of residual GP.
        temp_mean_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the mean of temperature parameters.
        temp_sdev_dict: (dict of tf.Variable) Dictionary of variational parameters for
            the stddev of temperature parameters.
        sigma_mean: (tf.Variable) Variational parameters for the mean of obs noise.
        sigma_sdev: (tf.Variable) Variational parameters for the stddev of obs noise.
    """

    # temperature and base_weight gps
    (weight_gp_dict, temp_dict,
     weight_gp_mean_dict, weight_gp_vcov_dict,
     temp_mean_dict, temp_sdev_dict,) = (
        tail_free.variational_family(X, base_pred,
                                     family_tree=family_tree,
                                     gp_vi_family=gp_vi_family,
                                     ls=ls_weight, **kwargs))

    # residual gp
    resid_gp, resid_gp_mean, resid_gp_vcov = gp_vi_family(X, ls=ls_resid,
                                                          name='vi_ensemble_resid',
                                                          **kwargs)

    # observation noise
    sigma, sigma_mean, sigma_sdev = inference_util.scalar_gaussian_variational(name='sigma')

    return (weight_gp_dict, resid_gp, temp_dict, sigma,  # variational RVs
            weight_gp_mean_dict, weight_gp_vcov_dict,  # weight GP variational parameters
            resid_gp_mean, resid_gp_vcov,  # resid GP variational parameters
            temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
            sigma_mean, sigma_sdev  # obs noise variational parameters
            )


def variational_family_sample(n_sample,
                              weight_gp_mean_dict, weight_gp_vcov_dict,
                              temp_mean_dict, temp_sdev_dict,
                              resid_gp_mean, resid_gp_vcov,
                              sigma_mean, sigma_sdev,
                              gp_sample_func=gp.variational_mfvi_sample):
    """Samples from the variational family for adaptive ensemble.

    Args:
        n_sample: (int) Number of samples to draw from variational family.
        weight_gp_mean_dict: (dict of np.ndarray) Dictionary of variational parameters for
            the mean of node weight GP.
        weight_gp_vcov_dict: (dict of np.ndarray) Dictionary of variational parameters for
            the stddev or covariance matrix of node weight GP.
        temp_mean_dict: (dict of np.ndarray) Dictionary of variational parameters for
            the mean of temperature parameters.
        temp_sdev_dict: (dict of np.ndarray) Dictionary of variational parameters for
            the stddev of temperature parameters.
        resid_gp_mean: (np.ndarray of float32) Dictionary of variational parameters for
            the mean of residual GP.
        resid_gp_vcov: (np.ndarray of float32) Dictionary of variational parameters for
            the stddev or covariance matrix of residual GP.
        sigma_mean: (float32) Variational parameters for the mean of obs noise.
        sigma_sdev: (float32) Variational parameters for the stddev of obs noise.
        gp_sample_func: (function): Sampling function for Gaussian Process variational family.

    Returns:
        weight_gp_sample_dict: (dict of tf.Tensor) Dictionary of temperature random variables
            for each parent model.
        temp_sample_dict: (dict of tf.Tensor) Dictionary of GP samples of raw weights
            for each non-root model/model family.
        resid_gp_sample: (tf.Tensor) GP samples for residual process.
        sigma_sample: (tf.Tensor) Samples of observation noise.
    """
    # sample model weight gp and temperature.
    weight_gp_sample_dict, temp_sample_dict = (
        tail_free.variational_family_sample(n_sample,
                                            weight_gp_mean_dict, weight_gp_vcov_dict,
                                            temp_mean_dict, temp_sdev_dict,
                                            gp_sample_func=gp_sample_func))
    # sample residual process gp
    resid_gp_sample = gp_sample_func(n_sample, resid_gp_mean, resid_gp_vcov)

    # sample observational noise
    sigma_sample = inference_util.scalar_gaussian_variational_sample(
        n_sample, sigma_mean, sigma_sdev)

    return weight_gp_sample_dict, temp_sample_dict, resid_gp_sample, sigma_sample


variational_mfvi = functools.partial(variational_family,
                                     gp_vi_family=gp.variational_mfvi)
variational_sgpr = functools.partial(variational_family,
                                     gp_vi_family=gp.variational_sgpr)

variational_mfvi_sample = functools.partial(variational_family_sample,
                                            gp_sample_func=gp.variational_mfvi_sample)
variational_sgpr_sample = functools.partial(variational_family_sample,
                                            gp_sample_func=gp.variational_sgpr_sample)
