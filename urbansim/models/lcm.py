"""
Use the ``MNLLocationChoiceModel`` class to train a choice module using
multinomial logit and make subsequent choice predictions.

"""
from __future__ import print_function, division

import numpy as np
import pandas as pd
from patsy import dmatrix
from prettytable import PrettyTable

from . import util
from ..urbanchoice import interaction, mnl
from ..utils import yamlio


def unit_choice(chooser_ids, alternative_ids, probabilities):
    """
    Have a set of choosers choose from among alternatives according
    to a probability distribution. Choice is binary: each
    alternative can only be chosen once.

    Parameters
    ----------
    chooser_ids : 1d array_like
        Array of IDs of the agents that are making choices.
    alternative_ids : 1d array_like
        Array of IDs of alternatives among which agents are making choices.
    probabilities : 1d array_like
        The probability that an agent will choose an alternative.
        Must be the same shape as `alternative_ids`. Unavailable
        alternatives should have a probability of 0.

    Returns
    -------
    choices : pandas.Series
        Mapping of chooser ID to alternative ID. Some choosers
        will map to a nan value when there are not enough alternatives
        for all the choosers.

    """
    chooser_ids = np.asanyarray(chooser_ids)
    alternative_ids = np.asanyarray(alternative_ids)
    probabilities = np.asanyarray(probabilities)

    choices = pd.Series([np.nan] * len(chooser_ids), index=chooser_ids)

    if probabilities.sum() == 0:
        # return all nan if there are no available units
        return choices

    # probabilities need to sum to 1 for np.random.choice
    probabilities = probabilities / probabilities.sum()

    # need to see if there are as many available alternatives as choosers
    n_available = np.count_nonzero(probabilities)
    n_choosers = len(chooser_ids)
    n_to_choose = n_choosers if n_choosers < n_available else n_available

    chosen = np.random.choice(
        alternative_ids, size=n_to_choose, replace=False, p=probabilities)

    # if there are fewer available units than choosers we need to pick
    # which choosers get a unit
    if n_to_choose == n_available:
        chooser_ids = np.random.choice(
            chooser_ids, size=n_to_choose, replace=False)

    choices[chooser_ids] = chosen

    return choices


class MNLLocationChoiceModel(object):
    """
    A location choice model with the ability to store an estimated
    model and predict new data based on the model.
    Based on multinomial logit.

    Parameters
    ----------
    model_expression : str, iterable, or dict
        A patsy model expression. Should contain only a right-hand side.
    sample_size : int
        Number of choices to sample for estimating the model.
    choosers_fit_filters : list of str, optional
        Filters applied to choosers table before fitting the model.
    choosers_predict_filters : list of str, optional
        Filters applied to the choosers table before calculating
        new data points.
    alts_fit_filters : list of str, optional
        Filters applied to the alternatives table before fitting the model.
    alts_predict_filters : list of str, optional
        Filters applied to the alternatives table before calculating
        new data points.
    interaction_predict_filters : list of str, optional
        Filters applied to the merged choosers/alternatives table
        before predicting agent choices.
    estimation_sample_size : int, optional, whether to sample choosers
        during estimation (needs to be applied after choosers_fit_filters)
    choice_column : optional
        Name of the column in the `alternatives` table that choosers
        should choose. e.g. the 'building_id' column. If not provided
        the alternatives index is used.
    name : optional
        Optional descriptive name for this model that may be used
        in output.

    """
    def __init__(self, model_expression, sample_size,
                 choosers_fit_filters=None, choosers_predict_filters=None,
                 alts_fit_filters=None, alts_predict_filters=None,
                 interaction_predict_filters=None,
                 estimation_sample_size=None,
                 choice_column=None, name=None):
        self.model_expression = model_expression
        self.sample_size = sample_size
        self.choosers_fit_filters = choosers_fit_filters
        self.choosers_predict_filters = choosers_predict_filters
        self.alts_fit_filters = alts_fit_filters
        self.alts_predict_filters = alts_predict_filters
        self.interaction_predict_filters = interaction_predict_filters
        self.estimation_sample_size = estimation_sample_size
        self.choice_column = choice_column
        self.name = name if name is not None else 'MNLLocationChoiceModel'

        self.log_likelihoods = None
        self.fit_parameters = None

    @classmethod
    def from_yaml(cls, yaml_str=None, str_or_buffer=None):
        """
        Create a LocationChoiceModel instance from a saved YAML configuration.
        Arguments are mutally exclusive.

        Parameters
        ----------
        yaml_str : str, optional
            A YAML string from which to load model.
        str_or_buffer : str or file like, optional
            File name or buffer from which to load YAML.

        Returns
        -------
        MNLLocationChoiceModel

        """
        cfg = yamlio.yaml_to_dict(yaml_str, str_or_buffer)

        model = cls(
            cfg['model_expression'],
            cfg['sample_size'],
            choosers_fit_filters=cfg.get('choosers_fit_filters', None),
            choosers_predict_filters=cfg.get('choosers_predict_filters', None),
            alts_fit_filters=cfg.get('alts_fit_filters', None),
            alts_predict_filters=cfg.get('alts_predict_filters', None),
            interaction_predict_filters=cfg.get(
                'interaction_predict_filters', None),
            estimation_sample_size=cfg.get('estimation_sample_size', None),
            choice_column=cfg.get('choice_column', None),
            name=cfg.get('name', None)
        )

        if cfg.get('log_likelihoods', None):
            model.log_likelihoods = cfg['log_likelihoods']
        if cfg.get('fit_parameters', None):
            model.fit_parameters = pd.DataFrame(cfg['fit_parameters'])

        return model

    @property
    def str_model_expression(self):
        """
        Model expression as a string suitable for use with patsy/statsmodels.

        """
        return util.str_model_expression(
            self.model_expression, add_constant=False)

    def fit(self, choosers, alternatives, current_choice):
        """
        Fit and save model parameters based on given data.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing,
            e.g. buildings.
        current_choice : pandas.Series or any
            A Series describing the `alternatives` currently chosen
            by the `choosers`. Should have an index matching `choosers`
            and values matching the index of `alternatives`.

            If a non-Series is given it should be a column in `choosers`.

        Returns
        -------
        log_likelihoods : dict
            Dict of log-liklihood values describing the quality of the
            model fit. Will have keys 'null', 'convergence', and 'ratio'.

        """
        if not isinstance(current_choice, pd.Series):
            current_choice = choosers[current_choice]

        choosers = util.apply_filter_query(choosers, self.choosers_fit_filters)

        if self.estimation_sample_size:
            choosers = choosers.loc[np.random.choice(
                choosers.index,
                min(self.estimation_sample_size, len(choosers)),
                replace=False)]

        current_choice = current_choice.loc[choosers.index]
        alternatives = util.apply_filter_query(
            alternatives, self.alts_fit_filters)
        _, merged, chosen = interaction.mnl_interaction_dataset(
            choosers, alternatives, self.sample_size, current_choice)
        model_design = dmatrix(
            self.str_model_expression, data=merged, return_type='dataframe')
        self.log_likelihoods, self.fit_parameters = mnl.mnl_estimate(
            model_design.as_matrix(), chosen, self.sample_size)
        self.fit_parameters.index = model_design.columns
        return self.log_likelihoods

    @property
    def fitted(self):
        """
        True if model is ready for prediction.

        """
        return self.fit_parameters is not None

    def assert_fitted(self):
        """
        Raises `RuntimeError` if the model is not ready for prediction.

        """
        if not self.fitted:
            raise RuntimeError('Model has not been fit.')

    def report_fit(self):
        """
        Print a report of the fit results.

        """
        if not self.fitted:
            print('Model not yet fit.')
            return

        print('Null Log-liklihood: {0:.3f}'.format(
            self.log_likelihoods['null']))
        print('Log-liklihood at convergence: {0:.3f}'.format(
            self.log_likelihoods['convergence']))
        print('Log-liklihood Ratio: {0:.3f}\n'.format(
            self.log_likelihoods['ratio']))

        tbl = PrettyTable(
            ['Component', ])
        tbl = PrettyTable()

        tbl.add_column('Component', self.fit_parameters.index.values)
        for col in ('Coefficient', 'Std. Error', 'T-Score'):
            tbl.add_column(col, self.fit_parameters[col].values)

        tbl.align['Component'] = 'l'
        tbl.float_format = '.3'

        print(tbl)

    def predict(self, choosers, alternatives):
        """
        Choose from among alternatives for a group of agents.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
            Only the first item in this table is used for determining
            agent probabilities of choosing alternatives.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing.

        Returns
        -------
        choices : pandas.Series
            Mapping of chooser ID to alternative ID. Some choosers
            will map to a nan value when there are not enough alternatives
            for all the choosers.

        """
        self.assert_fitted()

        choosers = util.apply_filter_query(
            choosers, self.choosers_predict_filters)
        alternatives = util.apply_filter_query(
            alternatives, self.alts_predict_filters)

        # TODO: only using 1st item in choosers for determining probabilities.
        # Need to expand options around this.
        _, merged, _ = interaction.mnl_interaction_dataset(
            choosers.head(1), alternatives, len(alternatives))
        merged = util.apply_filter_query(
            merged, self.interaction_predict_filters)
        model_design = dmatrix(
            self.str_model_expression, data=merged, return_type='dataframe')

        # probabilities are returned from mnl_simulate as a 2d array
        # and need to be flatted for use in unit_choice.
        probabilities = mnl.mnl_simulate(
            model_design.as_matrix(),
            self.fit_parameters['Coefficient'].values,
            numalts=len(merged), returnprobs=True).flatten()

        # figure out exactly which things from which choices are drawn
        alt_choices = (
            merged[self.choice_column] if self.choice_column else merged.index)

        return unit_choice(
            choosers.index, alt_choices, probabilities)

    def to_dict(self):
        """
        Return a dict respresentation of an MNLLocationChoiceModel
        instance.

        """
        return {
            'model_type': 'locationchoice',
            'model_expression': self.model_expression,
            'sample_size': self.sample_size,
            'name': self.name,
            'choosers_fit_filters': self.choosers_fit_filters,
            'choosers_predict_filters': self.choosers_predict_filters,
            'alts_fit_filters': self.alts_fit_filters,
            'alts_predict_filters': self.alts_predict_filters,
            'interaction_predict_filters': self.interaction_predict_filters,
            'estimation_sample_size': self.estimation_sample_size,
            'choice_column': self.choice_column,
            'fitted': self.fitted,
            'log_likelihoods': self.log_likelihoods,
            'fit_parameters': (yamlio.frame_to_yaml_safe(self.fit_parameters)
                               if self.fitted else None)
        }

    def to_yaml(self, str_or_buffer=None):
        """
        Save a model respresentation to YAML.

        Parameters
        ----------
        str_or_buffer : str or file like, optional
            By default a YAML string is returned. If a string is
            given here the YAML will be written to that file.
            If an object with a ``.write`` method is given the
            YAML will be written to that object.

        Returns
        -------
        j : str
            YAML is string if `str_or_buffer` is not given.

        """
        return yamlio.convert_to_yaml(self.to_dict(), str_or_buffer)


class MNLLocationChoiceModelGroup(object):
    """
    Manages a group of location choice models that refer to different
    segments of choosers.

    Model names must match the segment names after doing a pandas groupby.

    Parameters
    ----------
    segmentation_col
        Name of a column in the table of choosers. Will be used to perform
        a pandas groupby on the choosers table.

    """
    def __init__(self, segmentation_col):
        self.segmentation_col = segmentation_col
        self.models = {}

    def add_model(self, model):
        """
        Add an MNLLocationChoiceModel instance.

        Parameters
        ----------
        model : MNLLocationChoiceModel
            Should have a ``.name`` attribute matching one of the segments
            in the choosers table.

        """
        self.models[model.name] = model

    def add_model_from_params(
            self, name, model_expression, sample_size,
            choosers_fit_filters=None, choosers_predict_filters=None,
            alts_fit_filters=None, alts_predict_filters=None,
            interaction_predict_filters=None, estimation_sample_size=None,
            choice_column=None):
        """
        Add a model by passing parameters through to MNLLocationChoiceModel.

        Parameters
        ----------
        name
            Must match a segment in the choosers table.
        model_expression : str, iterable, or dict
            A patsy model expression. Should contain only a right-hand side.
        sample_size : int
            Number of choices to sample for estimating the model.
        choosers_fit_filters : list of str, optional
            Filters applied to choosers table before fitting the model.
        choosers_predict_filters : list of str, optional
            Filters applied to the choosers table before calculating
            new data points.
        alts_fit_filters : list of str, optional
            Filters applied to the alternatives table before fitting the model.
        alts_predict_filters : list of str, optional
            Filters applied to the alternatives table before calculating
            new data points.
        interaction_predict_filters : list of str, optional
            Filters applied to the merged choosers/alternatives table
            before predicting agent choices.
        estimation_sample_size : int, optional, whether to sample choosers
            during estimation (needs to be applied after choosers_fit_filters)
        choice_column : optional
            Name of the column in the `alternatives` table that choosers
            should choose. e.g. the 'building_id' column. If not provided
            the alternatives index is used.

        """
        self.models[name] = MNLLocationChoiceModel(
            model_expression, sample_size,
            choosers_fit_filters, choosers_predict_filters,
            alts_fit_filters, alts_predict_filters,
            interaction_predict_filters, estimation_sample_size,
            choice_column, name)

    def _iter_groups(self, data):
        """
        Iterate over the groups in `data` after grouping by
        `segmentation_col`. Skips any groups for which there
        is no model stored.

        Yields tuples of (name, df) where name is the group key
        and df is the group DataFrame.

        Parameters
        ----------
        data : pandas.DataFrame
            Must have a column with the same name as `segmentation_col`.

        """
        groups = data.groupby(self.segmentation_col)

        for name in self.models:
            yield name, groups.get_group(name)

    def fit(self, choosers, alternatives, current_choice):
        """
        Fit and save models based on given data after segmenting
        the `choosers` table.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
            Must have a column with the same name as the .segmentation_col
            attribute.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing,
            e.g. buildings.
        current_choice
            Name of column in `choosers` that indicates which alternative
            they have currently chosen.

        Returns
        -------
        log_likelihoods : dict of dict
            Keys will be model names and values will be dictionaries of
            log-liklihood values as returned by MNLLocationChoiceModel.fit.

        """
        return {name: self.models[name].fit(df, alternatives, current_choice)
                for name, df in self._iter_groups(choosers)}

    @property
    def fitted(self):
        """
        Whether all models in the group have been fitted.

        """
        return (all(m.fitted for m in self.models.values())
                if self.models else False)

    def predict(self, choosers, alternatives):
        """
        Choose from among alternatives for a group of agents after
        segmenting the `choosers` table.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
            Only the first item in this table is used for determining
            agent probabilities of choosing alternatives.
            Must have a column matching the .segmentation_col attribute.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing.

        Returns
        -------
        choices : pandas.Series
            Mapping of chooser ID to alternative ID. Some choosers
            will map to a nan value when there are not enough alternatives
            for all the choosers.

        """
        results = []

        for name, df in self._iter_groups(choosers):
            choices = self.models[name].predict(df, alternatives)
            # remove chosen alternatives
            alternatives = alternatives.loc[~alternatives.index.isin(choices)]
            results.append(choices)

        return pd.concat(results)


class SegmentedMNLLocationChoiceModel(object):
    """
    An MNL LCM group that allows segments to have different model expressions
    but otherwise share configurations.

    Parameters
    ----------
    segmentation_col
        Name of column in the choosers table that will be used for groupby.
    sample_size : int
        Number of choices to sample for estimating the model.
    choosers_fit_filters : list of str, optional
        Filters applied to choosers table before fitting the model.
    choosers_predict_filters : list of str, optional
        Filters applied to the choosers table before calculating
        new data points.
    alts_fit_filters : list of str, optional
        Filters applied to the alternatives table before fitting the model.
    alts_predict_filters : list of str, optional
        Filters applied to the alternatives table before calculating
        new data points.
    interaction_predict_filters : list of str, optional
        Filters applied to the merged choosers/alternatives table
        before predicting agent choices.
    estimation_sample_size : int, optional, whether to sample choosers
        during estimation (needs to be applied after choosers_fit_filters)
    choice_column : optional
        Name of the column in the `alternatives` table that choosers
        should choose. e.g. the 'building_id' column. If not provided
        the alternatives index is used.
    default_model_expr : str, iterable, or dict, optional
        A patsy model expression. Should contain only a right-hand side.

    """
    def __init__(self, segmentation_col, sample_size,
                 choosers_fit_filters=None, choosers_predict_filters=None,
                 alts_fit_filters=None, alts_predict_filters=None,
                 interaction_predict_filters=None,
                 estimation_sample_size=None,
                 choice_column=None, default_model_expr=None):
        self.segmentation_col = segmentation_col
        self.sample_size = sample_size
        self.choosers_fit_filters = choosers_fit_filters
        self.choosers_predict_filters = choosers_predict_filters
        self.alts_fit_filters = alts_fit_filters
        self.alts_predict_filters = alts_predict_filters
        self.interaction_predict_filters = interaction_predict_filters
        self.estimation_sample_size = estimation_sample_size
        self.choice_column = choice_column
        self.default_model_expr = default_model_expr
        self._group = MNLLocationChoiceModelGroup(segmentation_col)

    @classmethod
    def from_yaml(cls, yaml_str=None, str_or_buffer=None):
        """
        Create a SegmentedMNLLocationChoiceModel instance from a saved YAML
        configuration. Arguments are mutally exclusive.

        Parameters
        ----------
        yaml_str : str, optional
            A YAML string from which to load model.
        str_or_buffer : str or file like, optional
            File name or buffer from which to load YAML.

        Returns
        -------
        SegmentedMNLLocationChoiceModel

        """
        cfg = yamlio.yaml_to_dict(yaml_str, str_or_buffer)

        default_model_expr = cfg['default_config']['model_expression']

        seg = cls(
            cfg['segmentation_col'],
            cfg['sample_size'],
            cfg['choosers_fit_filters'],
            cfg['choosers_predict_filters'],
            cfg['alts_fit_filters'],
            cfg['alts_predict_filters'],
            cfg['interaction_predict_filters'],
            cfg['estimation_sample_size'],
            cfg['choice_column'],
            default_model_expr)

        if "models" not in cfg:
            cfg["models"] = {}

        for name, m in cfg['models'].items():
            m['model_expression'] = m.get(
                'model_expression', default_model_expr)
            m['sample_size'] = cfg['sample_size']
            m['choosers_fit_filters'] = None
            m['choosers_predict_filters'] = None
            m['alts_fit_filters'] = None
            m['alts_predict_filters'] = None
            m['interaction_predict_filters'] = \
                cfg['interaction_predict_filters']
            m['estimation_sample_size'] = cfg['estimation_sample_size']
            m['choice_column'] = cfg['choice_column']

            model = MNLLocationChoiceModel.from_yaml(
                yamlio.convert_to_yaml(m, None))
            seg._group.add_model(model)

        return seg

    def add_segment(self, name, model_expression=None):
        """
        Add a new segment with its own model expression.

        Parameters
        ----------
        name
            Segment name. Must match a segment in the groupby of the data.
        model_expression : str or dict, optional
            A patsy model expression that can be used with statsmodels.
            Should contain both the left- and right-hand sides.
            If not given the default model will be used, which must not be
            None.

        """
        if not model_expression:
            if not self.default_model_expr:
                raise ValueError(
                    'No default model available, '
                    'you must supply a model expression.')
            model_expression = self.default_model_expr

        # we'll take care of some of the filtering this side before
        # segmentation
        self._group.add_model_from_params(
            name=name,
            model_expression=model_expression,
            sample_size=self.sample_size,
            choosers_fit_filters=None,
            choosers_predict_filters=None,
            alts_fit_filters=None,
            alts_predict_filters=None,
            interaction_predict_filters=self.interaction_predict_filters,
            estimation_sample_size=self.estimation_sample_size,
            choice_column=self.choice_column)

    def fit(self, choosers, alternatives, current_choice):
        """
        Fit and save models based on given data after segmenting
        the `choosers` table. Segments that have not already been explicitly
        added will be automatically added with default model.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
            Must have a column with the same name as the .segmentation_col
            attribute.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing,
            e.g. buildings.
        current_choice
            Name of column in `choosers` that indicates which alternative
            they have currently chosen.

        Returns
        -------
        log_likelihoods : dict of dict
            Keys will be model names and values will be dictionaries of
            log-liklihood values as returned by MNLLocationChoiceModel.fit.

        """
        choosers = util.apply_filter_query(choosers, self.choosers_fit_filters)

        unique = choosers[self.segmentation_col].unique()

        # Remove any existing segments that may no longer have counterparts
        # in the data. This can happen when loading a saved model and then
        # calling this method with data that no longer has segments that
        # were there the last time this was called.
        gone = set(self._group.models) - set(unique)
        for g in gone:
            del self._group.models[g]

        for x in unique:
            if x not in self._group.models:
                self.add_segment(x)

        alternatives = util.apply_filter_query(
            alternatives, self.alts_fit_filters)

        return self._group.fit(choosers, alternatives, current_choice)

    @property
    def fitted(self):
        """
        Whether models for all segments have been fit.

        """
        return self._group.fitted

    def predict(self, choosers, alternatives):
        """
        Choose from among alternatives for a group of agents after
        segmenting the `choosers` table.

        Parameters
        ----------
        choosers : pandas.DataFrame
            Table describing the agents making choices, e.g. households.
            Only the first item in this table is used for determining
            agent probabilities of choosing alternatives.
            Must have a column matching the .segmentation_col attribute.
        alternatives : pandas.DataFrame
            Table describing the things from which agents are choosing.

        Returns
        -------
        choices : pandas.Series
            Mapping of chooser ID to alternative ID. Some choosers
            will map to a nan value when there are not enough alternatives
            for all the choosers.

        """
        choosers = util.apply_filter_query(
            choosers, self.choosers_predict_filters)
        alternatives = util.apply_filter_query(
            alternatives, self.alts_predict_filters)
        return self._group.predict(choosers, alternatives)

    def _process_model_dict(self, d):
        """
        Remove redundant items from a model's configuration dict.

        Parameters
        ----------
        d : dict
            Modified in place.

        Returns
        -------
        dict
            Modified `d`.

        """
        del d['model_type']
        del d['sample_size']
        del d['choosers_fit_filters']
        del d['choosers_predict_filters']
        del d['alts_fit_filters']
        del d['alts_predict_filters']
        del d['interaction_predict_filters']
        del d['estimation_sample_size']
        del d['choice_column']

        if d['model_expression'] == self.default_model_expr:
            del d['model_expression']

        d["name"] = yamlio.to_scalar_safe(d["name"])

        return d

    def to_dict(self):
        """
        Returns a dict representation of this instance suitable for
        conversion to YAML.

        """
        return {
            'model_type': 'segmented_locationchoice',
            'segmentation_col': self.segmentation_col,
            'sample_size': self.sample_size,
            'choosers_fit_filters': self.choosers_fit_filters,
            'choosers_predict_filters': self.choosers_predict_filters,
            'alts_fit_filters': self.alts_fit_filters,
            'alts_predict_filters': self.alts_predict_filters,
            'interaction_predict_filters': self.interaction_predict_filters,
            'estimation_sample_size': self.estimation_sample_size,
            'choice_column': self.choice_column,
            'default_config': {
                'model_expression': self.default_model_expr,
            },
            'fitted': self.fitted,
            'models': {yamlio.to_scalar_safe(name): self._process_model_dict(m.to_dict())
                       for name, m in self._group.models.items()}
        }

    def to_yaml(self, str_or_buffer=None):
        """
        Save a model respresentation to YAML.

        Parameters
        ----------
        str_or_buffer : str or file like, optional
            By default a YAML string is returned. If a string is
            given here the YAML will be written to that file.
            If an object with a ``.write`` method is given the
            YAML will be written to that object.

        Returns
        -------
        j : str
            YAML is string if `str_or_buffer` is not given.

        """
        return yamlio.convert_to_yaml(self.to_dict(), str_or_buffer)
