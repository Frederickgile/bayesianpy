# -*- coding: utf-8 -*-
"""
Created on Tue Apr 19 16:31:18 2016

@author: imorgan.admin
"""

from bayespy.jni import bayesServerInference

import bayespy.network
import pandas as pd
from bayespy.jni import bayesServer
from bayespy.jni import bayesServerParams
from bayespy.jni import jp
import numpy as np
import logging
import multiprocess.context as ctx
import pathos.multiprocessing as mp
import itertools

from typing import List


class QueryOutput:
    def __init__(self, continuous, discrete):
        self.continuous = continuous
        self.discrete = discrete


class QueryBase:
    def setup(self, network, inference_engine, query_options) -> None:
        pass

    def results(self, inference_engine, query_output) -> dict:
        pass

    def use_pandas(self):
        return True


class InferenceEngine:
    _inference_factory = None

    def __init__(self, network):
        self._network = network

    @staticmethod
    def get_inference_factory():
        if InferenceEngine._inference_factory is None:
            InferenceEngine._inference_factory = bayesServerInference().RelevanceTreeInferenceFactory()
        return InferenceEngine._inference_factory

    def create_engine(self):
        return self.get_inference_factory().createInferenceEngine(self._network)

    def create(self, loglikelihood=False, conflict=False, retract=False):
        query_options = self.get_inference_factory().createQueryOptions()
        query_output = self.get_inference_factory().createQueryOutput()
        inference_engine = self.get_inference_factory().createInferenceEngine(self._network)

        query_options.setLogLikelihood(loglikelihood)
        query_options.setConflict(conflict)

        if retract:
            query_options.setQueryEvidenceMode(bayesServerInference().QueryEvidenceMode.RETRACT_QUERY_EVIDENCE)

        return inference_engine, query_options, query_output


class SingleQuery:
    def __init__(self, network, inference_engine, logger):
        self._factory = bayesServerInference().RelevanceTreeInferenceFactory()
        self._query_options = self._factory.createQueryOptions()
        self._query_output = self._factory.createQueryOutput()
        self._inference_engine = inference_engine
        self._network = network
        self._logger = logger

    def query_as_df(self, queries: List[QueryBase], evidence=None, clear_evidence=True) -> pd.DataFrame:
        r = self.query(queries, evidence = evidence, clear_evidence = clear_evidence)
        if len(queries) == 1:
            return pd.DataFrame([r])
        else:
            return pd.DataFrame(r)

    def query(self, queries: List[QueryBase], evidence=None, clear_evidence=True):
        """
        Query a number of variables (if none, then query all variables in the network)
        :param variables: a list of variables, or none
        :return: a QueryOutput object with separate continuous/ discrete dataframes
        """
        for query in queries:
            query.setup(self._network, self._inference_engine, self._query_options)

        if evidence is not None:
            self._inference_engine.setEvidence(evidence)

        try:
            self._inference_engine.query(self._query_options, self._query_output)
        except BaseException as e:
            self._logger.error(e)

        results = []
        for query in queries:
            results.append(query.results(self._inference_engine, self._query_output))

        if clear_evidence:
            self._inference_engine.getEvidence().clear()

        if len(queries) == 1:
            return results[0]

        return results


class Evidence:
    def __init__(self, network, inference):
        self._network = network
        self._inference = inference
        self._evidence = inference.getEvidence()
        self._evidence.clear()
        self._variables = network.getVariables()

    def apply(self, evidence: dict):
        """
        Apply evidence to a network
        :param evidence: if a discrete variable, then a list of strings generated by network.Discrete.tostring(),
        if continuous, then a list of tuples with (VariableName, ContinuousValue)
        :return: Nothing
        """
        for key, value in evidence.items():
            v = self._variables.get(key)
            if bayespy.network.is_variable_discrete(v):
                if v is None:
                    raise ValueError("Node {} does not exist".format(key))

                st = v.getStates().get(value)
                if st is None:
                    raise ValueError("State {} does not exist in variable {}".format(value, key))

                self._evidence.setState(st)

            elif bayespy.network.is_variable_continuous(v):
                self._evidence.set(v, jp.java.lang.Double(value))


        return self._evidence


class QueryMixtureOfGaussians(QueryBase):
    def __init__(self, head_variables: List[str], tail_variables: List[str]):
        self._head_variables = head_variables
        self._tail_variables = tail_variables
        self._discrete_variables = []

    def setup(self, network, inference_engine, query_options):
        contexts = []
        for h in self._head_variables + self._tail_variables:
            v = bayespy.network.get_variable(network, h)

            if bayespy.network.is_variable_discrete(v):
                if h in self._head_variables:
                    raise ValueError("Bayespy only supports discrete tail variables (BayesServer is fine with it though!)")

                self._discrete_variables.append(v.getName())
            else:
                if h in self._tail_variables:
                    raise ValueError("Bayespy only supports continuous head variables (BayesServer is fine with it though!)")

            contexts.append(bayesServer().VariableContext(v, bayesServer().HeadTail.HEAD if h in self._head_variables
                                                                    else bayesServer().HeadTail.TAIL))

        self._network = network
        self._distribution = bayesServer().CLGaussian(contexts)
        self._query_distribution = bayesServerInference().QueryDistribution(self._distribution)
        inference_engine.getQueryDistributions().add(self._query_distribution)

    def _results(self, curr_tail_variables: List[str], inference_engine):
        pass

    def results(self, inference_engine, query_output):
        result = {}
        def state_generator(variables):
            for v in variables:
                tv = bayespy.network.get_variable(self._network, v)
                yield [state for state in tv.getStates()]

        def is_covariant():
            return len(self._head_variables) > 1

        for state_combinations in itertools.product(*state_generator(self._tail_variables)):

            state_name = "_".join([state.getName() for state in state_combinations])
            state_array = jp.JArray(state_combinations[0].getClass())(len(state_combinations))
            for i, state in enumerate(state_combinations):
                state_array[i] = state

            result.update({state_name: {}})
            if is_covariant():
                result[state_name].update({'mean': [], 'covariance': np.zeros((len(self._head_variables), len(self._head_variables)))})
            else:
                result[state_name].update({'variance': np.nan, 'mean': np.nan })

            for i,h in enumerate(self._head_variables):
                v = bayespy.network.get_variable(self._network, h)
                mean = self._distribution.getMean(v, state_array)
                if is_covariant():
                    result[state_name]['mean'].append(mean)
                    for j,h1 in enumerate(self._head_variables):
                        v1 = bayespy.network.get_variable(self._network, h1)
                        cov = self._distribution.getCovariance(v, v1, state_array)
                        result[state_name]['covariance'][i,j] = cov
                else:
                    result[state_name]['mean'] = mean
                    result[state_name]['variance'] = self._distribution.getVariance(v, state_array)

        return result


class QueryStatistics(QueryBase):
    def __init__(self, calc_loglikelihood=True, calc_conflict=False, loglikelihood_column='loglikelihood',
                 conflict_column='conflict'):
        self._calc_loglikelihood = calc_loglikelihood
        self._calc_conflict = calc_conflict
        self._loglikelihood_column = loglikelihood_column
        self._conflict_column = conflict_column

    def setup(self, network, inference_engine, query_options):
        query_options.setLogLikelihood(self._calc_loglikelihood)
        query_options.setConflict(self._calc_conflict)

    def results(self, inference_engine, query_output):
        result = {}
        if self._calc_loglikelihood:
            result.update({self._loglikelihood_column: query_output.getLogLikelihood().floatValue()})

        if self._calc_conflict:
            result.update({self._conflict_column: query_output.getConflict().floatValue()})

        return result


# seems like a better name than QueryStatistics, so just having this here.
class QueryModelStatistics(QueryStatistics):
    def __init__(self, calc_loglikelihood=True, calc_conflict=False, loglikelihood_column='loglikelihood',
                 conflict_column='conflict'):
        super().__init__(calc_loglikelihood, calc_conflict, loglikelihood_column, conflict_column)


class QueryMostLikelyState(QueryBase):
    def __init__(self, target_variable_name, output_dtype="object", suffix="_maxlikelihood"):
        self._target_variable_name = target_variable_name
        self._distribution = None
        self._output_dtype = output_dtype
        self._suffix = suffix

    def setup(self, network, inference_engine, query_options):
        distribution = None

        self._variable = bayespy.network.get_variable(network, self._target_variable_name)

        if bayespy.network.is_variable_discrete(self._variable):
            distribution = bayesServer().Table(self._variable)

        if distribution is None:
            raise ValueError("{} needs to be discrete in QueryMostLikelyState".format(self._target_variable_name))

        query_options.setQueryEvidenceMode(bayesServerInference().QueryEvidenceMode.RETRACT_QUERY_EVIDENCE)
        qd = bayesServerInference().QueryDistribution(distribution)

        self._distribution = distribution
        inference_engine.getQueryDistributions().add(qd)

    def results(self, inference_engine, query_output):
        states = {}
        for state in self._variable.getStates():
            states.update({state.getName(): self._distribution.get([state])})

        # get the most likely state
        max_state = max(states.keys(), key=(lambda key: states[key]))
        max_state_name = bayespy.data.DataFrame.cast2(self._output_dtype, max_state)

        return {self._target_variable_name + self._suffix: max_state_name}

class QueryStateProbability(QueryMostLikelyState):

    def __init__(self, target_variable_name, suffix="_probability"):
        super().__init__(target_variable_name=target_variable_name, output_dtype="float64", suffix=suffix)

    def results(self, inference_engine, query_output):
        states = {}
        for state in self._variable.getStates():
            p = self._distribution.get([state])
            states.update({self._target_variable_name + bayespy.network.STATE_DELIMITER + state.getName()
                           + self._suffix: p})

        return states


class QueryLogLikelihood(QueryBase):
    def __init__(self, variable_names, column_name: str = '_loglikelihood'):
        if isinstance(variable_names, str):
            variable_names = [variable_names]

        self._variable_names = variable_names
        self._distribution = None
        self._query_distribution = None

        self._column_name = column_name

    def setup(self, network, inference_engine, query_options):
        variables = [bayespy.network.get_variable(network, n) for n in self._variable_names]
        if len(variables) == 1:
            self._distribution = bayesServer().CLGaussian(variables[0])
        else:
            self._distribution = bayesServer().CLGaussian(variables)

        query_options.setQueryEvidenceMode(bayesServerInference().QueryEvidenceMode.RETRACT_QUERY_EVIDENCE)
        qd = bayesServerInference().QueryDistribution(self._distribution)
        qd.setQueryLogLikelihood(True)
        self._query_distribution = qd
        inference_engine.getQueryDistributions().add(qd)

    def results(self, inference_engine, query_output):
        result = {}
        ll = self._query_distribution.getLogLikelihood()
        value = ll.floatValue() if ll is not None else np.nan
        result.update({":".join(self._variable_names) + self._column_name: value})
        return result


class QueryMeanVariance(QueryBase):
    def __init__(self, variable_name, retract_evidence=True, result_mean_suffix='_mean',
                 result_variance_suffix='_variance', output_dtype=None):
        self._variable_name = variable_name

        self._result_mean_suffix = result_mean_suffix
        self._result_variance_suffix = result_variance_suffix
        self._retract_evidence = retract_evidence
        self._output_dtype = output_dtype

    def setup(self, network, inference_engine, query_options):
        self._variable = bayespy.network.get_variable(network, self._variable_name)

        if not bayespy.network.is_variable_continuous(self._variable):
            raise ValueError("{} needs to be continuous.".format(self._variable_name))

        self._query = bayesServer().CLGaussian(self._variable)

        if self._retract_evidence:
            query_options.setQueryEvidenceMode(bayesServerInference().QueryEvidenceMode.RETRACT_QUERY_EVIDENCE)

        inference_engine.getQueryDistributions().add(bayesServerInference().QueryDistribution(self._query))

    def results(self, inference_engine, query_output):
        mean = self._query.getMean(self._variable)
        if self._output_dtype is not None:
            mean = bayespy.data.DataFrame.cast2(self._output_dtype, mean)

        return {self._variable_name + self._result_mean_suffix: mean,
                self._variable_name + self._result_variance_suffix: self._query.getVariance(self._variable)}


def _batch_query(df: pd.DataFrame, connection_string: str, network: str, table_name: str,
                 variable_references: List[str],
                 queries, logger, i):
    bayespy.jni.attach(logger, heap_space='1g')
    data_reader = bayesServer().data.DatabaseDataReaderCommand(
        connection_string,
        "select * from {} where ix in ({})".format(table_name,
                                                   ",".join(str(i) for i in df.index.tolist()))).executeReader()

    network = bayespy.network.create_network_from_string(network)
    reader_options = bayesServer().data.ReaderOptions("ix")
    variable_refs = list(bayespy.network.create_variable_references(network, df,
                                                                    variable_references=variable_references))

    reader = bayesServer().data.DefaultEvidenceReader(data_reader, jp.java.util.Arrays.asList(variable_refs),
                                                      reader_options)

    factory = InferenceEngine(network)
    (inference_engine, query_options, query_output) = factory.create()

    for query in queries:
        query.setup(network, inference_engine, query_options)

    results = []
    try:
        while reader.read(inference_engine.getEvidence(), bayesServer().data.DefaultReadOptions(True)):
            result = {}

            try:
                inference_engine.query(query_options, query_output)
            except BaseException as e:
                logger.error(e)
                # inference_engine.getEvidence().clear()
                # continue

            for query in queries:
                result = {**result, **query.results(inference_engine, query_output)}

            inference_engine.getEvidence().clear()
            result.update({'caseid': int(reader.getReadInfo().getCaseId().toString())})

            results.append(result)

            if i % 500 == 0:
                logger.info("Queried case {}".format(i))

            i += 1
    finally:
        reader.close()
        # bayespy.jni.detach()
    return results


class BatchQuery:
    def __init__(self, network, datastore, logger: logging.Logger):

        self._logger = logger
        self._datastore = datastore
        # serialise the network as a string.
        self._network = network.saveToString()

    def _calc_num_threads(self, df_size: int, query_size: int) -> int:
        num_queries = df_size * query_size

        if mp.cpu_count() == 1:
            max = 1
        else:
            max = mp.cpu_count() - 1

        calc = int(num_queries / 5000)
        if calc > max:
            r = max
        elif calc <= 1:
            if num_queries > 1000:
                r = 2
            else:
                r = 1
        else:
            r = calc

        return r

    def query(self, queries: List[QueryBase] = [QueryStatistics()], append_to_df=True,
              variable_references: List[str] = []):

        if not hasattr(queries, "__getitem__"):
            queries = [queries]

        nt = self._network
        logger = self._logger
        conn = self._datastore.get_connection()
        table = self._datastore.table
        processes = self._calc_num_threads(len(self._datastore.data), len(queries))

        self._logger.info("Using {} processes to query {} rows".format(processes, len(self._datastore.data)))

        if processes == 1:
            pdf = pd.DataFrame(_batch_query(self._datastore.data, conn, nt, table,
                                            variable_references, queries,
                                            logger, 0))
        else:
            # bit nasty, but the only way I could get jpype to stop hanging in Linux.
            ctx._force_start_method('spawn')

            with mp.Pool(processes=processes) as pool:
                pdf = pd.DataFrame()
                for result_set in pool.map(lambda df: _batch_query(df, conn, nt, table,
                                                                   variable_references, queries,
                                                                   logger, 0),
                                           np.array_split(self._datastore.data, processes)):
                    pdf = pdf.append(pd.DataFrame(result_set))

        df = pdf.set_index('caseid')

        if append_to_df:
            return self._datastore.data.join(df)
        else:
            return df

class TrainingResults:
    def __init__(self, network, results: dict, logger: logging.Logger):
        self._network = network
        self._metrics = results
        self._logger = logger

    def get_metrics(self) -> dict:
        return self._metrics

    def get_network(self):
        return self._network

    def get_model(self):
        return NetworkModel(self._network, self._logger)


class Sampling:
    def __init__(self, network):
        self._network = network
        self._sampling = bayespy.jni.bayesServerSampling().DataSampler(self._network)

    def sample(self, num_samples: int=1):
        rand = jp.java.util.Random()
        evidence = bayespy.jni.bayesServerInference().DefaultEvidence(self._network)
        options = bayespy.jni.bayesServerSampling().DataSamplingOptions()
        results = []
        for i in range(num_samples):
            self._sampling.takeSample(evidence, rand, options)
            r = {}
            for variable in self._network.getVariables():
                v = evidence.get(variable)
                r.update({variable.getName(): v.floatValue() if v is not None else np.nan})

            results.append(r)

        return pd.DataFrame(results)

class NetworkModel:
    def __init__(self, network, logger):
        self._jnetwork = network
        self._inference_factory = InferenceEngine(network)
        self._logger = logger

    def get_network(self):
        return self._jnetwork

    def save(self, path):
        from xml.dom import minidom
        nt = self._jnetwork.saveToString()
        reparsed = minidom.parseString(nt)
        with open(path, 'w') as fh:
            fh.write(reparsed.toprettyxml(indent="  "))

    def is_trained(self):
        return bayespy.network.is_trained(self._jnetwork)

    def train(self, dataset: bayespy.data.DataSet) -> TrainingResults:
        """
        Train a model on data provided in the constructor
        """
        learning = bayesServerParams().ParameterLearning(self._jnetwork,
                                                         self._inference_factory.get_inference_factory())
        learning_options = bayesServerParams().ParameterLearningOptions()

        data_reader_command = dataset.create_data_reader_command()

        reader_options = bayesServer().data.ReaderOptions()

        variable_references = list(bayespy.network.create_variable_references(self._jnetwork, dataset.get_dataframe()))

        evidence_reader_command = bayesServer().data.DefaultEvidenceReaderCommand(data_reader_command,
                                                                                  jp.java.util.Arrays.asList(
                                                                                      variable_references),
                                                                                  reader_options)

        self._logger.info("Training model...")
        result = learning.learn(evidence_reader_command, learning_options)
        self._logger.info("Finished training model")

        return TrainingResults(self._jnetwork, {'converged': result.getConverged(),
                'loglikelihood': result.getLogLikelihood().floatValue(),
                'iteration_count': result.getIterationCount(), 'case_count': result.getCaseCount(),
                'weighted_case_count': result.getWeightedCaseCount(),
                'unweighted_case_count': result.getUnweightedCaseCount(),
                'bic': result.getBIC().floatValue()}, self._logger)

    def batch_query(self, dataset: bayespy.data.DataSet, queries: List[QueryBase], append_to_df=True,
                    variable_references: List[str] = []):
        bq = BatchQuery(self._jnetwork, dataset, self._logger)
        return bq.query(queries, append_to_df=append_to_df, variable_references=variable_references)
