import asyncio
import collections.abc
import dataclasses
import json
import re
from typing import List, Optional, Tuple

import aiohttp
import httpstan.models
import httpstan.schemas
import httpstan.services.arguments as arguments
import httpstan.utils
import numpy as np
import simdjson
from clikit.io import ConsoleIO
from clikit.ui.components import ProgressBar

import stan.common
import stan.fit


def _make_json_serializable(data: dict) -> dict:
    """Convert `data` with numpy.ndarray-like values to JSON-serializable form.

    Returns a new dictionary.

    Arguments:
        data (dict): A Python dictionary or mapping providing the data for the
            model. Variable names are the keys and the values are their
            associated values. Default is an empty dictionary.

    Returns:
        dict: Copy of `data` dict with JSON-serializable values.
    """
    # no need for deep copy, we do not modify mutable items
    data = data.copy()
    for key, value in data.items():
        # first, see if the value is already JSON-serializable
        try:
            json.dumps(value)
        except TypeError:
            pass
        else:
            continue
        # numpy scalar
        if isinstance(value, np.ndarray) and value.ndim == 0:
            data[key] = np.asarray(value).tolist()
        # numpy.ndarray, pandas.Series, and anything similar
        elif isinstance(value, collections.abc.Collection):
            data[key] = np.asarray(value).tolist()
        else:
            raise TypeError(f"Value associated with variable `{key}` is not JSON serializable.")
    return data


@dataclasses.dataclass(frozen=True)
class Model:
    """Stores data associated with and proxies calls to a Stan model.

    Returned by ``build``. Users will not instantiate this class directly.

    """

    model_name: str
    program_code: str
    data: dict
    param_names: Tuple[str, ...]
    constrained_param_names: Tuple[str, ...]
    dims: Tuple[Tuple[int, ...]]
    random_seed: Optional[int]

    def __post_init__(self):
        if self.model_name != httpstan.models.calculate_model_name(self.program_code):
            raise ValueError("`model_name` does not match `program_code`.")

    def sample(self, **kwargs) -> stan.fit.Fit:
        """Draw samples from the model.

        Parameters in ``kwargs`` will be passed to the default sample function in
        stan::services. Parameter names are identical to those used in CmdStan.
        See the CmdStan documentation for parameter descriptions and default
        values.

        `num_chains` is the lone PyStan-specific keyword argument. It indicates
        the number of independent processes to use when drawing samples.
        The default value is 4.

        Returns:
            Fit: instance of Fit allowing access to draws.

        """
        assert "chain" not in kwargs, "`chain` id is set automatically."
        assert "data" not in kwargs, "`data` is set in `build`."
        assert "random_seed" not in kwargs, "`random_seed` is set in `build`."

        num_chains = kwargs.pop("num_chains", 4)

        init = kwargs.pop("init", [dict() for _ in range(num_chains)])
        if len(init) != num_chains:
            raise ValueError("Initial values must be provided for each chain.")

        payloads = []
        for chain in range(1, num_chains + 1):
            payload = {"function": "stan::services::sample::hmc_nuts_diag_e_adapt"}
            payload.update(kwargs)
            payload["chain"] = chain  # type: ignore
            payload["data"] = self.data  # type: ignore
            payload["init"] = init.pop(0)
            if self.random_seed is not None:
                payload["random_seed"] = self.random_seed  # type: ignore

            # fit needs to know num_samples, num_warmup, num_thin, save_warmup
            # progress bar needs to know some of these
            num_warmup = payload.get("num_warmup", arguments.lookup_default(arguments.Method["SAMPLE"], "num_warmup"))
            num_samples = payload.get(
                "num_samples",
                arguments.lookup_default(arguments.Method["SAMPLE"], "num_samples"),
            )
            num_thin = payload.get("num_thin", arguments.lookup_default(arguments.Method["SAMPLE"], "num_thin"))
            save_warmup = payload.get(
                "save_warmup",
                arguments.lookup_default(arguments.Method["SAMPLE"], "save_warmup"),
            )
            payloads.append(payload)

        async def go():
            io = ConsoleIO()
            io.error_line("<info>Sampling...</info>")
            progress_bar = ProgressBar(io)
            progress_bar.set_format("very_verbose")

            current_and_max_iterations_re = re.compile(r"Iteration:\s+(\d+)\s+/\s+(\d+)")
            async with stan.common.httpstan_server() as (host, port):
                fits_url = f"http://{host}:{port}/v1/{self.model_name}/fits"
                operations = []
                for payload in payloads:
                    async with aiohttp.request("POST", fits_url, json=payload) as resp:
                        if resp.status == 422:
                            raise ValueError(str(await resp.json()))
                        elif resp.status != 201:
                            raise RuntimeError((await resp.json())["message"])
                        assert resp.status == 201
                        operations.append(await resp.json())

                # poll to get progress for each chain until all chains finished
                current_iterations = {}
                while not all(operation["done"] for operation in operations):
                    for operation in operations:
                        if operation["done"]:
                            continue
                        operation_name = operation["name"]
                        async with aiohttp.request("GET", f"http://{host}:{port}/v1/{operation_name}") as resp:
                            operation.update(await resp.json())
                            progress_message = operation["metadata"].get("progress")
                            if not progress_message:
                                continue
                            iteration, iteration_max = map(
                                int, current_and_max_iterations_re.findall(progress_message).pop(0)
                            )
                            if not progress_bar.get_max_steps():  # i.e., has not started
                                progress_bar.start(max=iteration_max * num_chains)
                            current_iterations[operation["name"]] = iteration
                            progress_bar.set_progress(sum(current_iterations.values()))
                    await asyncio.sleep(0.01)
                # Sampling has finished. But we do not call `progress_bar.finish()` right
                # now. First we write informational messages to the screen, then we
                # redraw the (complete) progress bar. Only after that do we call `finish`.

                stan_outputs = []
                for operation in operations:
                    fit_name = operation["result"].get("name")
                    if fit_name is None:  # operation["result"] is an error
                        assert not str(operation["result"]["code"]).startswith("2"), operation
                        raise RuntimeError(operation["result"]["message"])
                    async with aiohttp.request("GET", f"http://{host}:{port}/v1/{fit_name}") as resp:
                        if resp.status != 200:
                            raise RuntimeError((await resp.json())["message"])
                        stan_outputs.append(await resp.read())

                    # clean up after ourselves when fit is uncacheable (no random seed)
                    if self.random_seed is None:
                        async with aiohttp.request("DELETE", f"http://{host}:{port}/v1/{fit_name}") as resp:
                            if resp.status not in {200, 202, 204}:
                                raise RuntimeError((await resp.json())["message"])

                stan_outputs = tuple(stan_outputs)  # Fit constructor expects a tuple.

                def is_nonempty_logger_message(msg: simdjson.Object):
                    return msg["topic"] == "logger" and msg["values"][0] != "info:"

                def is_iteration_or_elapsed_time_logger_message(msg: simdjson.Object):
                    # Assumes `msg` is a message with topic `logger`.
                    text = msg["values"][0]
                    return (
                        text.startswith("info:Iteration:")
                        or text.startswith("info: Elapsed Time:")
                        # this detects lines following "Elapsed Time:", part of a multi-line Stan message
                        or text.startswith("info:" + " " * 15)
                    )

                parser = simdjson.Parser()
                nonstandard_logger_messages = []
                for stan_output in stan_outputs:
                    for line in stan_output.splitlines():
                        # Do not attempt to parse non-logger messages. Draws could contain nan or inf values.
                        # simdjson cannot parse lines containing such values.
                        if b'"logger"' not in line:
                            continue
                        msg = parser.parse(line)
                        if is_nonempty_logger_message(msg) and not is_iteration_or_elapsed_time_logger_message(msg):
                            nonstandard_logger_messages.append(msg.as_dict())
                del parser  # simdjson.Parser is no longer used at this point.

                progress_bar.clear()
                io.error("\x08" * progress_bar._last_messages_length)  # move left to start of line
                if nonstandard_logger_messages:
                    io.error_line("<comment>Messages received during sampling:</comment>")
                    for msg in nonstandard_logger_messages:
                        text = msg["values"][0].replace("info:", "  ").replace("error:", "  ")
                        if text.strip():
                            io.error_line(f"{text}")
                progress_bar.display()  # re-draw the (complete) progress bar
                progress_bar.finish()
                io.error_line("\n<info>Done.</info>")

            return stan.fit.Fit(
                stan_outputs,
                num_chains,
                self.param_names,
                self.constrained_param_names,
                self.dims,
                num_warmup,
                num_samples,
                num_thin,
                save_warmup,
            )

        try:
            return asyncio.run(go())
        except KeyboardInterrupt:
            return  # type: ignore

    def constrain_pars(
        self, unconstrained_parameters: List[float], include_tparams: bool = True, include_gqs: bool = True
    ) -> List[float]:
        """Transform a sequence of unconstrained parameters to their defined support,
           optionally including transformed parameters and generated quantities.

        Arguments:
            unconstrained_parameters: A sequence of unconstrained parameters.
            include_tparams: Boolean to control whether we include transformed parameters.
            include_gqs: Boolean to control whether we include generated quantities.

        Returns:
            A sequence of constrained parameters, optionally including transformed parameters.

        Note:
            The unconstrained parameters are passed to the `write_array` method of the `model_base`
            instance. See `model_base.hpp` in the Stan C++ library for details.
        """
        payload = {
            "data": self.data,
            "unconstrained_parameters": unconstrained_parameters,
            "include_tparams": include_tparams,
            "include_gqs": include_gqs,
        }

        async def go():
            async with stan.common.httpstan_server() as (host, port):
                write_array_url = f"http://{host}:{port}/v1/{self.model_name}/write_array"
                async with aiohttp.request("POST", write_array_url, json=payload) as resp:
                    response_payload = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(response_payload)
                    return (await resp.json())["params_r_constrained"]

        return asyncio.run(go())

    def unconstrain_pars(self, constrained_parameters: List[float]) -> List[float]:
        """Reads constrained parameter values from their specified context and returns a
           sequence of unconstrained parameter values.

        Arguments:
            constrained_parameters: Constrained parameter values and their specified context

        Returns:
            A sequence of unconstrained parameters.

        Note:
            The unconstrained parameters are passed to the `transform_inits` method of the
            `model_base` instance. See `model_base.hpp` in the Stan C++ library for details.
        """
        payload = {"data": self.data, "constrained_parameters": constrained_parameters}

        async def go():
            async with stan.common.httpstan_server() as (host, port):
                write_array_url = f"http://{host}:{port}/v1/{self.model_name}/transform_inits"
                async with aiohttp.request("POST", write_array_url, json=payload) as resp:
                    response_payload = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(response_payload)
                    return (await resp.json())["params_r_unconstrained"]

        return asyncio.run(go())

    def log_prob(self, unconstrained_parameters: List[float], adjust_transform: bool = True) -> float:
        """Calculate the log probability of a set of unconstrained parameters.

        Arguments:
            unconstrained_parameters: A sequence of unconstrained parameters.
            adjust_transform: Apply jacobian adjust transform.

        Returns:
            The log probability of the unconstrained parameters.

        Notes:
            The unconstrained parameters are passed to the log_prob
            function in stan::model.

        """
        payload = {
            "data": self.data,
            "unconstrained_parameters": unconstrained_parameters,
            "adjust_transform": adjust_transform,
        }

        async def go():
            async with stan.common.httpstan_server() as (host, port):
                log_prob_url = f"http://{host}:{port}/v1/{self.model_name}/log_prob"
                async with aiohttp.request("POST", log_prob_url, json=payload) as resp:
                    response_payload = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(response_payload)
                    return (await resp.json())["log_prob"]

        return asyncio.run(go())

    def grad_log_prob(self, unconstrained_parameters: List[float]) -> float:
        """Calculate the gradient of the log posterior evaluated at
           the unconstrained parameters.

        Arguments:
            unconstrained_parameters: A sequence of unconstrained parameters.
            adjust_transform: Apply jacobian adjust transform.

        Returns:
            The gradient of the log posterior evalauted at the
            unconstrained parameters.

        Notes:
            The unconstrained parameters are passed to the log_prob_grad
            function in stan::model.
        """
        assert isinstance(self.data, dict)

        payload = {
            "data": self.data,
            "unconstrained_parameters": unconstrained_parameters,
        }

        async def go():
            async with stan.common.httpstan_server() as (host, port):
                log_prob_grad_url = f"http://{host}:{port}/v1/{self.model_name}/log_prob_grad"
                async with aiohttp.request("POST", log_prob_grad_url, json=payload) as resp:
                    response_payload = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(response_payload)
                    return (await resp.json())["log_prob_grad"]

        return asyncio.run(go())


def build(program_code, data=None, random_seed=None) -> Model:
    """Build (compile) a Stan program.

    Arguments:
        program_code (str): Stan program code describing a Stan model.
        data (dict): A Python dictionary or mapping providing the data for the
            model. Variable names are the keys and the values are their
            associated values. Default is an empty dictionary.
        random_seed (int): Random seed, a positive integer for random number
            generation. Used to make sure that results can be reproduced.

    Returns:
        Model: an instance of Model

    Notes:
        C++ reserved words and Stan reserved words may not be used for
        variable names; see the Stan User's Guide for a complete list.

    """
    if data is None:
        data = {}
    # _make_json_serializable returns a new dict, original `data` unchanged
    data = _make_json_serializable(data)
    assert all(not isinstance(value, np.ndarray) for value in data.values())

    async def go():
        io = ConsoleIO()
        io.error("<info>Building...</info>")
        async with stan.common.httpstan_server() as (host, port):
            # Check to see if model is in cache.
            model_name = httpstan.models.calculate_model_name(program_code)
            path, payload = f"/v1/{model_name}/params", {"data": data}
            async with aiohttp.request("POST", f"http://{host}:{port}{path}", json=payload) as resp:
                model_in_cache = resp.status != 404
            io.error("\n" if model_in_cache else " This may take some time.\n")
            # Note: during compilation `httpstan` redirects stderr to /dev/null, making `print` impossible.
            path, payload = "/v1/models", {"program_code": program_code}
            async with aiohttp.request("POST", f"http://{host}:{port}{path}", json=payload) as resp:
                response_payload = await resp.json()
                if resp.status != 201:
                    raise RuntimeError(response_payload["message"])
                assert model_name == response_payload["name"]
                if response_payload.get("stanc_warnings"):
                    io.error_line("<comment>Messages from <fg=cyan;options=bold>stanc</>:</comment>")
                    io.error_line(response_payload["stanc_warnings"])
            path, payload = f"/v1/{model_name}/params", {"data": data}
            async with aiohttp.request("POST", f"http://{host}:{port}{path}", json=payload) as resp:
                if resp.status != 200:
                    raise RuntimeError((await resp.json())["message"])
                params_list = (await resp.json())["params"]
            assert len({param["name"] for param in params_list}) == len(params_list)
            param_names, dims = zip(*((param["name"], param["dims"]) for param in params_list))
            constrained_param_names = sum((tuple(param["constrained_names"]) for param in params_list), ())
            if model_in_cache:
                io.error("<comment>Found model in cache.</comment> ")
            io.error_line("<info>Done.</info>")
            return Model(model_name, program_code, data, param_names, constrained_param_names, dims, random_seed)

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return  # type: ignore
