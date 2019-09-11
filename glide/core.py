"""
Core classes used to power pipelines
"""
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from inspect import signature, Parameter

import climax
import numpy as np
import sqlite3
from toolbox import st, dbg, Script, Arg, MappingMixin, set_missing_key

from consecution import (
    Pipeline,
    GlobalState as ConsecutionGlobalState,
    Node as ConsecutionNode,
)
from glide.sql_utils import SQLALCHEMY_CONN_TYPES, is_sqlalchemy_conn, get_bulk_replace
from glide.utils import iterize, is_pandas

SCRIPT_DATA_ARG = "data"

# TODO: move to toolbox
Parent = climax.parent


class GlobalState(MappingMixin, ConsecutionGlobalState):
    """Consecution GlobalState with more dict-like behavior"""

    def __bool__(self):
        """Hack to get Consecution to use this as default even if technically empty"""
        return True


class Node(ConsecutionNode):
    """Override Consecution's Node class to add necessary functionality"""

    def __init__(self, name, **default_context):
        super().__init__(name)
        self.default_context = default_context
        self.reset_context()
        self.run_args, self.run_kwargs = self._get_run_args()

    def update_context(self, context):
        """Update the context dict for this Node"""
        self.context.update(context)

    def reset_context(self):
        """Reset context dict for this Node to the default"""
        self.context = self.default_context.copy()

    def _get_run_args(self):
        """Get the args and kwargs of this Node's run() method"""
        positionals = OrderedDict()
        keywords = OrderedDict()
        sig = signature(self.run)

        for i, param_name in enumerate(sig.parameters):
            param = sig.parameters[param_name]
            if i == 0:
                # The first param is the item to process which is passed
                # directly in process()
                continue
            if param.kind == param.POSITIONAL_ONLY:
                positionals[param.name] = None
            elif (
                param.default == Parameter.empty
                and param.kind == param.POSITIONAL_OR_KEYWORD
            ):
                positionals[param.name] = None
            elif param.kind == param.POSITIONAL_OR_KEYWORD:
                keywords[param.name] = param.default
            elif param.kind == param.VAR_KEYWORD:
                pass
            else:
                assert False, "%s params are not allowed in run()" % param.kind

        return positionals, keywords

    def _populate_run_args(self):
        """Populate the args to run() based on the current context"""
        _args = []
        for run_arg in self.run_args:
            if run_arg not in self.context:
                if self.global_state and run_arg in self.global_state:
                    # Use global_state as a backup for populating positional args
                    _args.append(self.global_state[run_arg])
                else:
                    raise Exception(
                        'Required run arg "%s" is missing from context: %s'
                        % (run_arg, self.context)
                    )
            else:
                _args.append(self.context[run_arg])

        # Everything else in the node context will be passed as part of kwargs
        # if it hasn't already been used in run_args
        _kwargs = {}
        for key in self.context:
            if key in self.run_args:
                continue
            _kwargs[key] = self.context[key]

        return _args, _kwargs

    def process(self, item):
        """Required method used by Consecution to process nodes"""
        _args, _kwargs = self._populate_run_args()
        self._run(item, *_args, **_kwargs)

    def _run(self, item, *args, **kwargs):
        self.run(item, *args, **kwargs)

    def run(self, item, *args, **kwargs):
        """Subclasses will override this method to implement core node logic """
        raise NotImplementedError


class DefaultNode(Node):
    """A default node that just passes all items through"""

    def run(self, item, **kwargs):
        self.push(item)


class PlaceholderNode(DefaultNode):
    """Used as a placeholder in pipelines. Will pass values through by default"""

    pass


class SkipFalseNode(Node):
    """This overrides the behavior of calling run() such that if a "false"
    object is pushed it will never call run, just push to next node instead"""

    def _run(self, item, *args, **kwargs):
        if is_pandas(item):
            if item.empty:
                self.push(item)
                return
        else:
            if not item:
                self.push(item)
                return
        self.run(item, *args, **kwargs)


class DataFramePushMixin:
    """Shared logic for DataFrame-based nodes"""

    def do_push(self, df, **kwargs):
        """Push the DataFrame to the next node, obeying chunksize if passed"""
        if kwargs.get("chunksize", None):
            for chunk in df:
                self.push(chunk)
        else:
            self.push(df)


class SQLCursorPushMixin:
    """Shared logic for SQL cursor-based nodes"""

    def do_push(self, cursor, chunksize=None):
        """Fetch data and push to the next node, obeying chunksize if passed"""
        if chunksize:
            while True:
                chunk = cursor.fetchmany(chunksize)
                if not chunk:
                    break
                self.push(chunk)
        else:
            data = cursor.fetchall()
            self.push(data)


class DataFramePushNode(Node, DataFramePushMixin):
    """Base class for DataFrame-based nodes"""

    pass


class BaseSQLConnectionNode(SkipFalseNode):
    """Base class for SQL-based nodes, checks for valid connection types on init"""

    allowed_conn_types = None

    def __init__(self, *args, **kwargs):
        assert self.allowed_conn_types and isinstance(
            self.allowed_conn_types, (list, tuple)
        ), (
            "%s.allowed_conn_types must be a list or tuple of connection types"
            % self.__class__.__name__
        )
        super().__init__(*args, **kwargs)

    def begin(self):
        conn = self.context.get("conn", None) or self.global_state.get("conn", None)
        assert conn, (
            "%s requires a conn argument in context or global state"
            % self.__class__.__name__
        )
        self.check_conn(conn)

    def _is_allowed_conn(self, conn):
        return isinstance(conn, tuple(self.allowed_conn_types))

    def check_conn(self, conn):
        assert self._is_allowed_conn(conn), (
            "Connection type %s is not in allowed types: %s"
            % (type(conn), self.allowed_conn_types)
        )


class PandasSQLConnectionNode(BaseSQLConnectionNode, DataFramePushMixin):
    """Captures the connection types allowed to work with Pandas to_sql/from_sql"""

    allowed_conn_types = SQLALCHEMY_CONN_TYPES + [sqlite3.Connection]


class SQLAlchemyConnectionNode(BaseSQLConnectionNode, SQLCursorPushMixin):
    """Captures the connection types allowed to work with SQLAlchemy"""

    allowed_conn_types = SQLALCHEMY_CONN_TYPES


class SQLiteConnectionNode(BaseSQLConnectionNode, SQLCursorPushMixin):
    """Captures the connection types allowed to work with SQLite"""

    allowed_conn_types = [sqlite3.Connection]


class SQLDBAPIConnectionNode(BaseSQLConnectionNode, SQLCursorPushMixin):
    """Checks that a valid DBAPI connection is passed"""

    allowed_conn_types = [object]

    def check_conn(self, conn):
        super().check_conn(conn)
        assert hasattr(conn, "cursor"), "DBAPI connections must have a cursor() method"


class SQLConnectionNode(BaseSQLConnectionNode, SQLCursorPushMixin):
    """A generic SQL node that will behave differently based on the conn type"""

    allowed_conn_types = [object]

    def check_conn(self, conn):
        """Make sure the object is a valid SQL connection"""
        assert hasattr(conn, "cursor") or is_sqlalchemy_conn(
            conn
        ), "Connection must have a cursor() method or be a SQLAlchemy connection"

    def get_sql_executor(self, conn):
        """Get the object that can execute queries"""
        if is_sqlalchemy_conn(conn):
            return conn
        return conn.cursor()

    def sql_execute(self, conn, cursor, sql, **kwargs):
        """Executes the sql statement and returns an object that can fetch results"""
        if is_sqlalchemy_conn(conn):
            qr = conn.execute(sql, **kwargs)
            return qr
        qr = cursor.execute(sql, **kwargs)
        return cursor

    def sql_executemany(self, conn, cursor, sql, rows):
        """Bulk executes the sql statement and returns an object that can fetch results"""
        if is_sqlalchemy_conn(conn):
            qr = conn.execute(sql, rows)
            return qr
        qr = cursor.executemany(sql, rows)
        return cursor

    def get_bulk_replace(self, conn, table, rows):
        """Get a bulk replace SQL statement"""
        if is_sqlalchemy_conn(conn):
            return get_bulk_replace(table, rows[0].keys(), dicts=False)
        if isinstance(conn, sqlite3.Connection):
            assert isinstance(
                rows[0], sqlite3.Row
            ), "Only sqlite3.Row rows are supported"
            return get_bulk_replace(
                table, rows[0].keys(), dicts=False, value_string="?"
            )
        assert not isinstance(
            rows[0], tuple
        ), "Dict rows expected, got tuple. Please use a dict cursor."
        return get_bulk_replace(table, rows[0].keys())


class Reducer(Node):
    """Waits until end() to call push(), effectively waiting for all nodes before
    it to finish before continuing the pipeline"""

    def begin(self):
        """Setup a place for results to be collected"""
        self.results = []

    def run(self, item, **kwargs):
        """Collect results from previous nodes"""
        self.results.append(item)

    def end(self):
        """Do the push once all results are in"""
        self.push(self.results)


class ThreadReducer(Reducer):
    """A plain-old Reducer with a name that makes it clear it works with threads"""

    pass


class FuturesPushNode(DefaultNode):
    """A node that either splits or duplicates its input to pass to multiple
    downstream nodes in parallel according to the executor_class that supports
    the futures interface.
    """

    executor_class = ProcessPoolExecutor
    as_completed_func = as_completed

    def _push(self, item):
        """Override Consecution's push such that we can push in parallel"""
        if self._logging == "output":
            self._write_log(item)

        executor_kwargs = self.context.get("executor_kwargs", None) or {}
        with self.executor_class(**executor_kwargs) as executor:
            futures = []

            if self.context.get("split", False):
                splits = np.array_split(item, len(self._downstream_nodes))
                for i, downstream in enumerate(self._downstream_nodes):
                    futures.append(executor.submit(downstream._process, splits[i]))
            else:
                for downstream in self._downstream_nodes:
                    futures.append(executor.submit(downstream._process, item))

            for future in self.__class__.as_completed_func(futures):
                result = future.result()


class ProcessPoolPush(FuturesPushNode):
    """A multi-process FuturesPushNode"""

    pass


class ThreadPoolPush(FuturesPushNode):
    """A multi-threaded FuturesPushNode"""

    executor_class = ThreadPoolExecutor


def update_node_contexts(pipeline, node_contexts):
    """Helper function for updating node contexts in a pipeline"""
    for k, v in node_contexts.items():
        assert k in pipeline._node_lookup, "Invalid node: %s" % k
        pipeline[k].update_context(v)


def reset_node_contexts(pipeline, node_contexts):
    """Helper function for resetting node contexts in a pipeline"""
    for k in node_contexts:
        assert k in pipeline._node_lookup, "Invalid node: %s" % k
        pipeline[k].reset_context()


def consume(pipeline, data, **node_contexts):
    """Handles node contexts before/after calling pipeline.consume()"""
    update_node_contexts(pipeline, node_contexts)
    pipeline.consume(iterize(data))
    reset_node_contexts(pipeline, node_contexts)


class Glider:
    """Main class for forming and executing pipelines. It thinly wraps
    Consecution's Pipeline, but does not subclass it due to a bug in pickle
    that hits an infinite recursion when using multiprocessing with a
    super().func reference.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the pipeline"""
        set_missing_key(
            kwargs, "global_state", GlobalState()
        )  # Ensure our version is default
        self.pipeline = Pipeline(*args, **kwargs)

    def __getitem__(self, name):
        """Passthrough to Consecution Pipeline"""
        return self.pipeline[name]

    def __setitem__(self, name_to_replace, replacement_node):
        """Passthrough to Consecution Pipeline"""
        self.pipeline[name_to_replace] = replacement_node

    def __str__(self):
        """Passthrough to Consecution Pipeline"""
        return self.pipeline.__str__()

    @property
    def global_state(self):
        return self.pipeline.global_state

    @global_state.setter
    def global_state(self, value):
        self.pipeline.global_state = value

    def consume(self, data, **node_contexts):
        """Setup node contexts and consume data with the pipeline"""
        consume(self.pipeline, data, **node_contexts)

    def plot(self, *args, **kwargs):
        """Passthrough to Consecution Pipeline.plot"""
        self.pipeline.plot(*args, **kwargs)

    def get_node_lookup(self):
        """Passthrough to Consecution Pipeline._node_lookup"""
        return self.pipeline._node_lookup

    def cli(self, *script_args, blacklist=None, parents=None, inject=None, clean=None):
        """Generate a decorator for this Glider that can be used to expose a CLI"""
        decorator = GliderScript(
            self,
            *script_args,
            blacklist=blacklist,
            parents=parents,
            inject=inject,
            clean=clean
        )
        return decorator


class ProcessPoolParaGlider(Glider):
    """A parallel Glider that uses a ProcessPoolExecutor to execute parallel calls to
    consume()"""

    def consume(self, data, **node_contexts):
        """Setup node contexts and consume data with the pipeline"""
        with ProcessPoolExecutor() as executor:
            splits = np.array_split(data, min(len(data), executor._max_workers))
            futures = []
            for split in splits:
                futures.append(
                    executor.submit(consume, self.pipeline, split, **node_contexts)
                )
            for future in as_completed(futures):
                result = future.result()


class ThreadPoolParaGlider(Glider):
    """A parallel Glider that uses a ThreadPoolExecutor to execute parallel calls to
    consume()"""

    def consume(self, data, **node_contexts):
        """Setup node contexts and consume data with the pipeline"""
        with ThreadPoolExecutor() as executor:
            splits = np.array_split(data, min(len(data), executor._max_workers))
            futures = []
            for split in splits:
                futures.append(
                    executor.submit(consume, self.pipeline, split, **node_contexts)
                )
            for future in as_completed(futures):
                result = future.result()


class GliderScript(Script):
    """A decorator that can be used to create a CLI from a Glider pipeline"""

    def __init__(
        self,
        glider,
        *script_args,
        blacklist=None,
        parents=None,
        inject=None,
        clean=None
    ):
        """Generate the script args for the given Glider and return a decorator"""
        self.glider = glider
        self.blacklist = set(blacklist or [])

        self.parents = parents or []
        assert isinstance(self.parents, list), (
            "parents must be a *list* of climax.parents: %s" % parents
        )

        self.inject = inject or {}
        if inject:
            assert isinstance(
                self.inject, dict
            ), "inject must be a dict of argname->func mappings"
            for injected_arg in inject:
                dbg("Adding injected_arg %s to blacklist" % injected_arg)
                self.blacklist.add(injected_arg)

        self.clean = clean or {}
        if clean:
            assert isinstance(
                self.clean, dict
            ), "clean must be a dict of argname->func mappings"

        script_args = self._get_script_args(script_args)
        return super().__init__(*script_args)

    def __call__(self, func, *args, **kwargs):
        func = self._node_arg_converter(func, *args, **kwargs)
        return super().__call__(func, *args, **kwargs)

    def get_injected_kwargs(self):
        """Override Script method to return populated kwargs from inject arg"""
        if not self.inject:
            return {}
        result = {}
        for key, func in self.inject.items():
            dbg("Injecting %s via %s" % (key, func))
            result[key] = func()
        return result

    def clean_up(self, **kwargs):
        """Override Script method to do any required clean up"""
        if not self.clean:
            return

        errors = []
        for key, func in self.clean.items():
            try:
                if key not in kwargs:
                    errors.append("Could not clean up %s, no arg found" % key)
                dbg("Calling clean_up function %s for %s=%s" % (func, key, kwargs[key]))
                func(kwargs[key])
            except:
                pass
        if errors:
            raise Exception("Errors during clean_up: %s" % errors)

    def blacklisted(self, node_name, arg_name):
        """Determine if an argument has been blacklisted from the CLI"""
        if arg_name in self.blacklist:
            return True
        if self._get_script_arg_name(node_name, arg_name) in self.blacklist:
            return True
        return False

    def _get_script_arg_name(self, node_name, arg_name):
        return "%s_%s" % (node_name, arg_name)

    def _get_script_arg(self, node, arg_name, required=False, default=None):
        """Generate a toolbox Arg"""
        if self.blacklisted(node.name, arg_name):
            return

        if arg_name in self.inject:
            required = False
            default = None
        elif arg_name in node.context:
            required = False
            default = node.context[arg_name]
        elif arg_name in self.glider.global_state:
            required = False
            default = self.glider.global_state[arg_name]

        arg_type = str
        if default:
            arg_type = type(default)

        arg_name = "--" + self._get_script_arg_name(node.name, arg_name)
        dbg(
            "Script arg: %s required:%s, type:%s, default:%s"
            % (arg_name, required, arg_type, default)
        )
        if arg_type == bool:
            action = "store_true"
            if default:
                action = "store_false"
            script_arg = Arg(
                arg_name, required=required, action=action, default=default
            )
        else:
            script_arg = Arg(
                arg_name, required=required, type=arg_type, default=default
            )
        return script_arg

    def _get_script_args(self, custom_script_args=None):
        """Generate all toolbox Args for this Glider"""
        node_lookup = self.glider.get_node_lookup()
        custom_script_args = custom_script_args or []
        script_args = OrderedDict()

        if not self.blacklisted("", SCRIPT_DATA_ARG):
            script_args[SCRIPT_DATA_ARG] = Arg(SCRIPT_DATA_ARG, nargs="+")

        for node in node_lookup.values():
            for arg_name, _ in node.run_args.items():
                script_arg = self._get_script_arg(node, arg_name, required=True)
                if not script_arg:
                    continue
                script_args[script_arg.name] = script_arg

            for kwarg_name, kwarg_default in node.run_kwargs.items():
                script_arg = self._get_script_arg(
                    node, kwarg_name, required=False, default=kwarg_default
                )
                if not script_arg:
                    continue
                script_args[script_arg.name] = script_arg

        for custom_arg in custom_script_args:
            assert not self.blacklisted("", custom_arg.name), (
                "Blacklisted arg '%s' passed as a custom arg" % custom_arg.name
            )
            if custom_arg.name in script_args:
                dbg("Overriding '%s' with custom arg" % custom_arg.name)
            script_args[custom_arg.name] = custom_arg

        return script_args.values()

    def _node_arg_converter(self, func, *args, **kwargs):
        """Wrap the wrapped function so we can convert from CLI keyword args to node
        contexts"""

        def inner(data, *args, **kwargs):
            nonlocal self
            kwargs = self._convert_kwargs(kwargs)
            return func(data, *args, **kwargs)

        return inner

    def _get_injected_node_contexts(self, kwargs):
        """Populate node contexts based on injected args"""
        node_contexts = {}
        node_lookup = self.glider.get_node_lookup()
        for node in node_lookup.values():
            for arg_name, _ in node.run_args.items():
                if arg_name in self.inject:
                    node_contexts.setdefault(node.name, {})[arg_name] = kwargs[arg_name]
            for kwarg_name, kwarg_default in node.run_kwargs.items():
                if kwarg_name in self.inject:
                    node_contexts.setdefault(node.name, {})[arg_name] = kwargs[
                        kwarg_name
                    ]
        return node_contexts

    def _convert_kwargs(self, kwargs):
        """Convert flat kwargs to node contexts and remaining kwargs"""
        nodes = self.glider.get_node_lookup()
        node_contexts = {}
        unused = set()

        for key, value in kwargs.items():
            key_parts = key.split("_")
            node_name = key_parts[0]
            if node_name not in nodes:
                unused.add(key)
                continue
            assert (
                len(key_parts) > 1
            ), "Invalid keyword arg %s, can not be a node name" % (key)
            arg_name = "_".join(key_parts[1:])
            node_contexts.setdefault(node_name, {})[arg_name] = value

        injected_node_contexts = self._get_injected_node_contexts(kwargs)
        for node_name, injected_args in injected_node_contexts.items():
            dbg("Injecting args for node %s: %s" % (node_name, injected_args))
            if node_name in node_contexts:
                node_contexts[node_name].update(injected_args)
            else:
                node_contexts[node_name] = injected_args

        final_kwargs = node_contexts
        for key in unused:
            final_kwargs[key] = kwargs[key]

        return final_kwargs
