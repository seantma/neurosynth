# emacs: -*- mode: python-mode; py-indent-offset: 2; tab-width: 2; indent-tabs-mode: nil -*-
# ex: set sts=2 ts=2 sw=2 et:

import logging
import numpy as np
from scipy import sparse
from scipy.stats import norm

from neurosynth.base import imageutils
import stats
import os

logger = logging.getLogger('neurosynth.meta')


def analyze_features(dataset, features, image_type='pFgA_z', threshold=0.001, q=0.01, save=None):
    """ Generate meta-analysis images for a set of features.
    Args:
        dataset: A Dataset instance containing feature and activation data.
        features: A list of named features to generate meta-analysis maps for.
        image_type: The type of image to return. Specify one of the extensions
            generated by the MetaAnalysis procedure--e.g., pFgA_z, pAgF, etc. By
            default, will use pFgA_z (i.e., z-scores reflecting the probability
            that a Mappable has a feature given that activation is present).
        threshold: The threshold for determining whether or not a Mappable has
            a feature. By default, this is 0.001, which is only sensible in the
            case of term-based features (so be sure to specify it for other kinds).
        q: The FDR rate to use for multiple comparisons correction (default = 0.05).
        save: Directory to save all meta-analysis images to. Images will be 
            created in this directory and prepended with the feature name.
            If none, returns all the data as a matrix.
    Returns:
        If save is None, an n_voxels x n_features 2D numpy array.
    """
    if save is None:
        result = np.zeros((dataset.masker.n_vox_in_mask, len(features)))
    else:
        result = []

    for i, f in enumerate(features):
        ids = dataset.get_ids_by_features(f, threshold=threshold)
        ma = MetaAnalysis(dataset, ids, q=q)
        if save is None:
            result[:, i] = ma.images[image_type]
        else:
            ma.save_results(output_dir=save, prefix=f)

    if save is None:
        return result

class MetaAnalysis(object):

    """ Meta-analysis of a Dataset. Currently contrasts two subsets of
    studies within a Dataset and saves a bunch of statistical images.
    Only one list of study IDs (ids) needs to be passed; the Universe will
    be bisected into studies that are and are not included in the
    list, and the contrast is then performed across these two groups.
    If a second optional second study list is provided (ids2), the Dataset
    is first constrained to the union of ids1 and ids2, and the standard
    contrast is then performed."""

    # DESPERATELY NEEDS REFACTORING!!!

    def __init__(self, dataset, ids, ids2=None, q=0.01, prior=0.5, min_studies=1):
        """ Initialize a new MetaAnalysis instance and run an analysis.
        Args:
            dataset: A Dataset instance.
            ids: A list of Mappable IDs to include in the meta-analysis.
            ids2: Optional second list of Mappable IDs. If passed, the set of studies will
                be restricted to the union of ids and ids2 before performing the meta-analysis.
                This is useful for meta-analytic contrasts, as the resulting images will in
                effect identify regions that are reported/activated more frequently in one
                list than in the other.
            q: The FDR threshold to use when correcting for multiple comparisons. Set to
                .01 by default.
            prior: The prior to use when calculating conditional probabilities. This is the
                prior probability of a feature being used in a study (i.e., p(F)). For example,
                if set to 0.25, the analysis will assume that 1/4 of studies load on the target
                feature, as opposed to the empirically estimated p(F), which is len(ids) /
                total number of studies in the dataset. If prior is not passed, defaults to 0.5,
                reflecting an effort to put all terms on level footing and avoid undue influence
                of base rates (because some terms are much more common than others). Note that
                modifying the prior will only affect the effect size/probability maps, and
                not the statistical inference (z-score) maps.
            min_studies: Integer or float indicating which voxels to mask out from results
                due to lack of stability. If an integer is passed, all voxels that activate
                in fewer than this number of studies will be ignored (i.e., a value of 0
                will be assigned in all output images). If a float in the range of 0 - 1 is
                passed, this will be interpreted as a proportion to use as the cut-off (e.g.,
                passing 0.03 will exclude all voxels active in fewer than 3% of the entire
                dataset). Defaults to 1, meaning all voxels that activate at least one study
                will be kept.
        """

        self.dataset = dataset
        mt = dataset.image_table
        self.selected_ids = list(set(mt.ids) & set(ids))
        self.selected_id_indices = np.in1d(mt.ids, ids)

        # If ids2 is provided, we only use mappables explicitly in either ids or ids2.
        # Otherwise, all mappables not in the ids list are used as the control
        # condition.
        unselected_id_indices = ~self.selected_id_indices if ids2 == None else np.in1d(
            mt.ids, ids2)

        # Calculate different count variables
        logger.debug("Calculating counts...")
        n_selected = len(self.selected_ids)
        n_unselected = np.sum(unselected_id_indices)
        n_mappables = n_selected + n_unselected

        n_selected_active_voxels = mt.data.dot(self.selected_id_indices)
        n_unselected_active_voxels = mt.data.dot(unselected_id_indices)

        # Nomenclature for variables below: p = probability, F = feature present, g = given,
        # U = unselected, A = activation. So, e.g., pAgF = p(A|F) = probability of activation
        # in a voxel if we know that the feature is present in a study.
        pF = (n_selected * 1.0) / n_mappables
        pA = np.array((mt.data.sum(axis=1) * 1.0) / n_mappables).squeeze()

        # Conditional probabilities
        logger.debug("Calculating conditional probabilities...")
        pAgF = n_selected_active_voxels * 1.0 / n_selected
        pAgU = n_unselected_active_voxels * 1.0 / n_unselected
        pFgA = pAgF * pF / pA

        # Recompute conditionals with uniform prior
        logger.debug("Recomputing with uniform priors...")
        pAgF_prior = prior * pAgF + (1 - prior) * pAgU
        pFgA_prior = pAgF * prior / pAgF_prior

        def p_to_z(p, sign):
            p = p/2  # convert to two-tailed
            # prevent underflow
            p[p < 1e-240] = 1e-240
            # Convert to z and assign tail
            z = np.abs(norm.ppf(p)) * sign
            # Set invalid voxels to 0
            z[np.isinf(z)] = 0.0
            return z
            
        # One-way chi-square test for consistency of activation
        p_vals = stats.one_way(np.squeeze(n_selected_active_voxels), n_selected)
        z_sign = np.sign(n_selected_active_voxels - np.mean(n_selected_active_voxels)).ravel()
        pAgF_z = p_to_z(p_vals, z_sign)
        fdr_thresh = stats.fdr(p_vals, q)
        pAgF_z_FDR = imageutils.threshold_img(pAgF_z, fdr_thresh, p_vals, mask_out='above')

        # Two-way chi-square for specificity of activation
        cells = np.squeeze(
            np.array([[n_selected_active_voxels, n_unselected_active_voxels],
                      [n_selected - n_selected_active_voxels, n_unselected - n_unselected_active_voxels]]).T)
        p_vals = stats.two_way(cells)
        z_sign = np.sign(pAgF - pAgU).ravel()
        pFgA_z = p_to_z(p_vals, z_sign)
        fdr_thresh = stats.fdr(p_vals, q)
        pFgA_z_FDR = imageutils.threshold_img(pFgA_z, fdr_thresh, p_vals, mask_out='above')

        # Retain any images we may want to save or access later
        self.images = {
            'pA': pA,
            'pAgF': pAgF,
            'pFgA': pFgA,
            ('pAgF_given_pF=%0.2f' % prior): pAgF_prior,
            ('pFgA_given_pF=%0.2f' % prior): pFgA_prior,
            'pAgF_z': pAgF_z,
            'pFgA_z': pFgA_z,
            ('pAgF_z_FDR_%s' % q): pAgF_z_FDR,
            ('pFgA_z_FDR_%s' % q): pFgA_z_FDR
        }

        # Mask out all voxels below num_studies threshold
        if min_studies > 0:
            if isinstance(min_studies, int):
                min_studies = float(
                    min_studies) / n_mappables  # Recalculate as proportion
            vox_to_exclude = np.where(pA < min_studies)[0]  # Create mask
            # Mask each image
            for k in self.images:
                self.images[k][vox_to_exclude] = 0

    def save_results(self, output_dir='.', prefix='', prefix_sep='_', image_list=None):
        """ Write out any images generated by the meta-analysis. 
        Args:
            output_dir: folder to write images to
            prefix: all image files will be prepended with this string
            prefix_sep: the character to glue the prefix and rest of filename with
            image_list: optional list of images to save--e.g., ['pFgA_z', 'pAgF'].
                If image_list is None (default), will save all images.
        """

        if prefix == '':
            prefix_sep = ''

        logger.debug("Saving results...")
        if image_list is None:
            image_list = self.images.keys()
        for suffix, img in self.images.items():
            if suffix in image_list:
                filename = prefix + prefix_sep + suffix + '.nii.gz'
                outpath = os.path.join(output_dir, filename)
                imageutils.save_img(img, outpath, self.dataset.masker)

