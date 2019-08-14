import asyncio
import logging
from json import JSONDecodeError
from types import FunctionType, CoroutineType
from typing import List, Union, Any, Callable, Type, Optional, Dict, Sequence, Awaitable

from pydantic import StrictStr, ValidationError, DictError, Schema
from pydantic import BaseModel
from pydantic.main import MetaModel
from fastapi.dependencies.models import Dependant
from fastapi.encoders import jsonable_encoder
from fastapi.params import Depends, Param
from fastapi import FastAPI
from fastapi.dependencies.utils import solve_dependencies, get_dependant, get_flat_dependant
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute, APIRouter, serialize_response
from starlette.background import BackgroundTasks
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, request_response, compile_path
import fastapi.params
import aiojobs


logger = logging.getLogger(__name__)


class Params(fastapi.params.Body):
    def __init__(
        self,
        default: Any,
        *,
        media_type: str = "application/json",
        alias: str = None,
        title: str = None,
        description: str = None,
        gt: float = None,
        ge: float = None,
        lt: float = None,
        le: float = None,
        min_length: int = None,
        max_length: int = None,
        regex: str = None,
        **extra: Any,
    ):
        super().__init__(
            default,
            embed=False,
            media_type=media_type,
            alias=alias,
            title=title,
            description=description,
            gt=gt,
            ge=ge,
            lt=lt,
            le=le,
            min_length=min_length,
            max_length=max_length,
            regex=regex,
            **extra,
        )


components = {}


def component_name(name: str, module: str = None):
    """OpenAPI components must be unique by name"""
    def decorator(obj):
        obj.__name__ = name
        if module is not None:
            obj.__module__ = module  # see: pydantic.schema.get_long_model_name
        key = (obj.__name__, obj.__module__)
        if key in components:
            if components[key].schema() != obj.schema():
                raise RuntimeError(
                    f'Different models with the same name detected: {obj!r} != {components[key]}'
                )
            return components[key]
        components[key] = obj
        return obj
    return decorator


def is_scope_child(owner: type, child: type):
    return (
        (
            owner.__dict__.get(child.__name__) is child or
            owner.__dict__.get(child.__name__) is Optional[child]
        ) and
        child.__qualname__ == owner.__qualname__ + '.' + child.__name__ and
        child.__module__ == owner.__module__
    )


def rename_if_scope_child_component(owner: type, child, postfix: str):
    if is_scope_child(owner, child):
        child = component_name(f'{owner.__name__}.{postfix}', owner.__module__)(child)
    return child


class BaseError(Exception):
    CODE = None
    MESSAGE = None

    ErrorModel = None
    DataModel = None

    data_required = False
    errors_required = False

    error_model = None
    data_model = None
    resp_model = None

    _component_name = None

    def __init__(self, data=None):
        if data is None:
            data = {}

        raw_data = data
        data = self.validate_data(raw_data)

        Exception.__init__(self, self.CODE, self.MESSAGE)

        self.data = data
        self.raw_data = raw_data

    @classmethod
    def validate_data(cls, data):
        data_model = cls.get_data_model()
        if data_model:
            data = data_model.validate(data)
        return data

    def __str__(self):
        s = f'[{self.CODE}] {self.MESSAGE}'
        if self.data:
            s += f': {self.data!r}'
        return s

    def get_resp_data(self):
        return self.raw_data

    @classmethod
    def get_description(cls):
        s = cls.get_default_description()
        if cls.__doc__:
            s += '\n\n' + cls.__doc__
        return s

    @classmethod
    def get_default_description(cls):
        return f'[{cls.CODE}] {cls.MESSAGE}'

    def get_resp(self):
        error = {
            'code': self.CODE,
            'message': self.MESSAGE,
        }

        resp_data = self.get_resp_data()
        if resp_data:
            error['data'] = resp_data

        resp = {
            'jsonrpc': '2.0',
            'error': error,
            'id': None,
        }

        return jsonable_encoder(resp)

    @classmethod
    def get_error_model(cls):
        if cls.__dict__.get('error_model') is not None:
            return cls.error_model
        cls.error_model = cls.build_error_model()
        return cls.error_model

    @classmethod
    def build_error_model(cls):
        if cls.ErrorModel is not None:
            return rename_if_scope_child_component(cls, cls.ErrorModel, 'Error')
        return None

    @classmethod
    def get_data_model(cls):
        if cls.__dict__.get('data_model') is not None:
            return cls.data_model
        cls.data_model = cls.build_data_model()
        return cls.data_model

    @classmethod
    def build_data_model(cls):
        if cls.DataModel is not None:
            return rename_if_scope_child_component(cls, cls.DataModel, 'Data')

        error_model = cls.get_error_model()
        if error_model is None:
            return None

        errors_annotation = List[error_model]
        if not cls.errors_required:
            errors_annotation = Optional[errors_annotation]

        ns = {
            '__annotations__': {
                'errors': errors_annotation,
            }
        }

        _ErrorData = MetaModel.__new__(MetaModel, '_ErrorData', (BaseModel, ), ns)
        _ErrorData = component_name(f'_ErrorData[{error_model.__name__}]', error_model.__module__)(_ErrorData)

        return _ErrorData

    @classmethod
    def get_resp_model(cls):
        if cls.__dict__.get('resp_model') is not None:
            return cls.resp_model
        cls.resp_model = cls.build_resp_model()
        return cls.resp_model

    @classmethod
    def build_resp_model(cls):
        ns = {
            'code': Schema(cls.CODE, const=True, example=cls.CODE),
            'message': Schema(cls.MESSAGE, const=True, example=cls.MESSAGE),
            '__annotations__': {
                'code': int,
                'message': str,
            }
        }

        data_model = cls.get_data_model()
        if data_model is not None:
            if not cls.data_required:
                data_model = Optional[data_model]
            # noinspection PyTypeChecker
            ns['__annotations__']['data'] = data_model

        name = cls._component_name or cls.__name__

        _JsonRpcErrorModel = MetaModel.__new__(MetaModel, '_JsonRpcErrorModel', (BaseModel, ), ns)
        _JsonRpcErrorModel = component_name(name, cls.__module__)(_JsonRpcErrorModel)

        @component_name(f'_ErrorResponse[{name}]', cls.__module__)
        class _ErrorResponseModel(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            error: _JsonRpcErrorModel

            class Config:
                extra = 'forbid'

        return _ErrorResponseModel


@component_name('_Error')
class ErrorModel(BaseModel):
    loc: List[str]
    msg: str
    type: str
    ctx: Optional[Dict[str, Any]]


class ParseError(BaseError):
    """Invalid JSON was received by the server"""
    CODE = -32700
    MESSAGE = 'Parse error'


class InvalidRequest(BaseError):
    """The JSON sent is not a valid Request object"""
    CODE = -32600
    MESSAGE = 'Invalid Request'
    error_model = ErrorModel


class MethodNotFound(BaseError):
    """The method does not exist / is not available"""
    CODE = -32601
    MESSAGE = 'Method not found'


class InvalidParams(BaseError):
    """Invalid method parameter(s)"""
    CODE = -32602
    MESSAGE = 'Invalid params'
    error_model = ErrorModel


class InternalError(BaseError):
    """Internal JSON-RPC error"""
    CODE = -32603
    MESSAGE = 'Internal error'


class NoContent(Exception):
    pass


async def call_sync_async(call, *args, **kwargs):
    is_coroutine = asyncio.iscoroutinefunction(call)
    if is_coroutine:
        return await call(*args, **kwargs)
    else:
        return await run_in_threadpool(call, *args, **kwargs)


def errors_responses(errors: Sequence[Type[BaseError]] = None):
    responses = {}

    if errors:
        cnt = 1
        for error_cls in errors:
            responses[f'200{" " * cnt}'] = {
                'model': error_cls.get_resp_model(),
                'description': error_cls.get_description(),
            }
            cnt += 1

    return responses


@component_name(f'_Request')
class JsonRpcRequest(BaseModel):
    jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
    id: Union[StrictStr, int] = Schema(None, example=0)
    method: StrictStr
    params: dict

    class Config:
        extra = 'forbid'


@component_name(f'_Response')
class JsonRpcResponse(BaseModel):
    jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
    id: Union[StrictStr, int] = Schema(None, example=0)
    result: dict

    class Config:
        extra = 'forbid'


def validation_error(
    exc: ValidationError,
    error_factory: Type[BaseError]
) -> BaseError:
    errors = []

    for err in exc.errors():
        if err['loc'][:1] == ('body', ):
            err['loc'] = err['loc'][1:]
        errors.append(err)

    error = error_factory(data={'errors': errors})

    return error


def fix_query_dependencies(dependant: Dependant):
    dependant.body_params.extend(dependant.query_params)
    dependant.query_params = []

    for field in dependant.body_params:
        if not isinstance(field.schema, Params):
            field.schema.embed = True

    for sub_dependant in dependant.dependencies:
        fix_query_dependencies(sub_dependant)


class MethodRoute(APIRoute):
    def __init__(
        self,
        entrypoint: 'Entrypoint',
        path: str,
        func: Union[FunctionType, CoroutineType],
        *,
        result_model: Type[Any] = None,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        shared_dependencies: list = None,
        **kwargs,
    ):
        name = name or func.__name__
        result_model = result_model or func.__annotations__.get('return')

        _, path_format, _ = compile_path(path)
        func_dependant = get_dependant(path=path_format, call=func)
        fix_query_dependencies(func_dependant)
        flat_dependant = get_flat_dependant(func_dependant)

        whole_params_list = [p for p in flat_dependant.body_params if isinstance(p.schema, Params)]
        if len(whole_params_list):
            if len(whole_params_list) > 1:
                raise RuntimeError(
                    f"Only one 'Params' schema allowed: "
                    f"params={whole_params_list}"
                )
            body_params_list = [p for p in flat_dependant.body_params if not isinstance(p.schema, Params)]
            if body_params_list:
                raise RuntimeError(
                    f"No other params allowed when 'Params' schema used: "
                    f"params={whole_params_list}, other={body_params_list}"
                )

        if whole_params_list:
            _JsonRpcRequestParams = whole_params_list[0].type_
            params_schema = whole_params_list[0].schema
        else:
            ns = {field.name: field.schema for field in flat_dependant.body_params}
            ns['__annotations__'] = {field.name: field.type_ for field in flat_dependant.body_params}

            _JsonRpcRequestParams = MetaModel.__new__(MetaModel, '_JsonRpcRequestParams', (BaseModel, ), ns)
            _JsonRpcRequestParams = component_name(f'_Params[{name}]', func.__module__)(_JsonRpcRequestParams)

            params_schema = Schema(...)

        @component_name(f'_Request[{name}]', func.__module__)
        class _Request(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            method: StrictStr = Schema(name, const=True, example=name)
            params: _JsonRpcRequestParams = params_schema

            class Config:
                extra = 'forbid'

        @component_name(f'_Response[{name}]', func.__module__)
        class _Response(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            result: result_model

            class Config:
                extra = 'forbid'

        # Only needed to generate OpenAPI
        async def endpoint(__request__: _Request):
            del __request__

        endpoint.__name__ = func.__name__
        endpoint.__doc__ = func.__doc__

        responses = errors_responses(errors)

        super().__init__(
            path,
            endpoint,
            methods=['POST'],
            name=name,
            response_model=_Response,
            response_model_skip_defaults=True,
            responses=responses,
            **kwargs,
        )

        self.func = func
        self.func_dependant = func_dependant
        self.entrypoint = entrypoint
        self.app = request_response(self.handle_http_request)
        self.shared_dependencies = shared_dependencies

    async def parse_body(self, http_request) -> Any:
        try:
            req = await http_request.json()
        except JSONDecodeError:
            raise ParseError()
        return req

    async def handle_http_request(self, http_request: Request):
        background_tasks = BackgroundTasks()

        # There may be exceptions to the transport layer, we don’t wrap them in JSON-RPC
        dependency_cache = await self.entrypoint.solve_shared_dependencies(http_request, background_tasks)

        try:
            body = await self.parse_body(http_request)
        except Exception as exc:
            resp = await self.entrypoint.handle_exception_to_resp(exc)
        else:
            try:
                resp = await self.handle_req_to_resp(http_request, background_tasks, dependency_cache, body)
            except NoContent:
                # no content for successful notifications
                return Response(media_type='application/json', background=background_tasks)

        return self.response_class(content=resp, background=background_tasks)

    async def handle_req_to_resp(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache: dict,
        req: Any,
    ) -> dict:
        handler_coro = self.handle_req(http_request, background_tasks, dependency_cache, req)
        return await self.entrypoint.handle_req_to_resp(handler_coro, req)

    async def handle_req(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache: dict,
        req: Any,
    ):
        try:
            JsonRpcRequest.validate(req)
        except DictError:
            raise InvalidRequest(data={'errors': [{
                'loc': (),
                'type': 'type_error.dict',
                'msg': "value is not a valid dict",
            }]})
        except ValidationError as exc:
            raise validation_error(exc, InvalidRequest)

        if req['method'] != self.name:
            raise MethodNotFound

        # dependency_cache - these are transport-layer dependencies, we pass them to each method, since
        # they are common to all methods in the batch.
        # But if the methods have their own dependencies, they are resolved separately.
        dependency_cache = dependency_cache.copy()

        values, errors, background_tasks, sub_response, _ = await solve_dependencies(
            request=http_request,
            dependant=self.func_dependant,
            body=req['params'],
            background_tasks=background_tasks,
            dependency_overrides_provider=self.dependency_overrides_provider,
            dependency_cache=dependency_cache,
        )

        if errors:
            raise validation_error(RequestValidationError(errors), InvalidParams)

        result = await call_sync_async(self.func, **values)

        response = {
            'jsonrpc': '2.0',
            'result': result,
            'id': req.get('id')
        }

        # noinspection PyTypeChecker
        resp = serialize_response(
            field=self.secure_cloned_response_field,
            response=response,
            include=self.response_model_include,
            exclude=self.response_model_exclude,
            by_alias=self.response_model_by_alias,
            skip_defaults=self.response_model_skip_defaults,
        )

        return resp


class EntrypointRoute(APIRoute):
    def __init__(
        self,
        entrypoint: 'Entrypoint',
        path: str,
        *,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        **kwargs,
    ):
        name = name or 'entrypoint'

        # This is only necessary for generating OpenAPI
        def endpoint(__request__: JsonRpcRequest):
            del __request__

        responses = errors_responses(errors)

        super().__init__(
            path,
            endpoint,
            methods=['POST'],
            name=name,
            response_model=JsonRpcResponse,
            responses=responses,
            **kwargs,
        )

        self.app = request_response(self.handle_http_request)
        self.entrypoint = entrypoint

    async def solve_dependencies(self, http_request: Request, background_tasks: BackgroundTasks) -> dict:
        # Must not be empty, otherwise FastAPI re-creates it
        dependency_cache = {(lambda: None, ('', )): 1}
        if self.dependencies:
            await solve_dependencies(
                request=http_request,
                dependant=self.dependant,
                body=None,
                background_tasks=background_tasks,
                dependency_overrides_provider=self.dependency_overrides_provider,
                dependency_cache=dependency_cache,
            )
        return dependency_cache

    async def parse_body(self, http_request) -> Any:
        try:
            body = await http_request.json()
        except JSONDecodeError:
            raise ParseError()

        if isinstance(body, list) and not body:
            raise InvalidRequest(data={'errors': [
                {'loc': (), 'type': 'value_error.empty', 'msg': 'rpc call with an empty array'}
            ]})

        return body

    async def handle_http_request(self, http_request: Request):
        background_tasks = BackgroundTasks()

        # There may be exceptions to the transport layer, we don’t wrap them in JSON-RPC
        dependency_cache = await self.solve_dependencies(http_request, background_tasks)

        try:
            body = await self.parse_body(http_request)
        except Exception as exc:
            resp = await self.entrypoint.handle_exception_to_resp(exc)
        else:
            try:
                resp = await self.handle_body(http_request, background_tasks, dependency_cache, body)
            except NoContent:
                # no content for successful notifications
                return Response(media_type='application/json', background=background_tasks)

        return self.response_class(content=resp, background=background_tasks)

    async def handle_body(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache: dict,
        body: Any,
    ):
        scheduler = await self.entrypoint.get_scheduler()

        if isinstance(body, list):
            req_list = body
        else:
            req_list = [body]

        job_list = []
        if len(req_list) > 1:
            # Run concurrently through scheduler
            for req in req_list:
                job = await scheduler.spawn(
                    self.handle_req_to_resp(http_request, background_tasks, dependency_cache, req)
                )

                # TODO: https://github.com/aio-libs/aiojobs/issues/119
                job._explicit = True
                # noinspection PyProtectedMember
                coro = job._do_wait(timeout=None)

                job_list.append(coro)
        else:
            req = req_list[0]
            coro = self.handle_req_to_resp(http_request, background_tasks, dependency_cache, req)
            job_list.append(coro)

        resp_list = []

        for coro in job_list:
            try:
                resp = await coro
            except NoContent:
                # No response for successful notifications
                continue

            resp_list.append(resp)

        if not resp_list:
            raise NoContent

        if not isinstance(body, list):
            content = resp_list[0]
        else:
            content = resp_list

        return content

    async def handle_req_to_resp(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache: dict,
        req: Any,
    ) -> dict:
        handler_coro = self.handle_req(http_request, background_tasks, dependency_cache, req)
        return await self.entrypoint.handle_req_to_resp(handler_coro, req)

    async def handle_req(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache: dict,
        req: Any,
    ):
        try:
            JsonRpcRequest.validate(req)
        except DictError:
            raise InvalidRequest(data={'errors': [
                {'loc': (), 'type': 'type_error.dict', 'msg': 'value is not a valid dict'}
            ]})
        except ValidationError as exc:
            raise validation_error(exc, InvalidRequest)

        scope = http_request.scope.copy()
        scope['path'] = self.path + '/' + req['method']

        for route in self.entrypoint.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                # http_request is a transport layer and it is common for all JSON-RPC requests in a batch
                return await route.handle_req(http_request, background_tasks, dependency_cache, req)
        else:
            raise MethodNotFound()


class Entrypoint(APIRouter):
    method_route_class = MethodRoute
    entrypoint_route_class = EntrypointRoute

    default_errors: Sequence[Type[BaseError]] = [
        InvalidParams, MethodNotFound, ParseError, InvalidRequest, InternalError,
    ]

    def __init__(
        self,
        path: str,
        *,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        scheduler_factory: Callable[..., Awaitable[aiojobs.Scheduler]] = aiojobs.create_scheduler,
        scheduler_kwargs: dict = None,
        **kwargs,
    ) -> None:
        super().__init__(redirect_slashes=False)
        if errors is None:
            errors = self.default_errors
        self.scheduler_factory = scheduler_factory
        self.scheduler_kwargs = scheduler_kwargs
        self.scheduler = None
        self.entrypoint_route = self.entrypoint_route_class(
            self,
            path,
            name=name,
            errors=errors,
            **kwargs,
        )
        self.routes.append(self.entrypoint_route)

    async def shutdown(self):
        if self.scheduler is not None:
            await self.scheduler.close()

    async def get_scheduler(self):
        if self.scheduler is not None:
            return self.scheduler
        self.scheduler = await self.scheduler_factory(**(self.scheduler_kwargs or {}))
        return self.scheduler

    async def handle_exception(self, exc):
        raise exc

    async def handle_exception_to_resp(self, exc) -> dict:
        try:
            resp = await self.handle_exception(exc)
        except BaseError as error:
            resp = error.get_resp()
        except Exception as exc:
            logger.exception(str(exc), exc_info=exc)
            resp = InternalError().get_resp()
        return resp

    async def handle_req_to_resp(self, handler_coro: Awaitable, req: Any) -> dict:
        try:
            resp = await handler_coro
        except Exception as exc:
            resp = await self.handle_exception_to_resp(exc)

        # empty response for successful notifications
        has_content = 'error' in resp or 'id' in req

        if not has_content:
            raise NoContent

        if isinstance(req, dict):
            resp['id'] = req.get('id')
        else:
            resp['id'] = None

        return resp

    def bind_dependency_overrides_provider(self, value):
        for route in self.routes:
            route.dependency_overrides_provider = value

    async def solve_shared_dependencies(self, http_request: Request, background_tasks: BackgroundTasks) -> dict:
        return await self.entrypoint_route.solve_dependencies(http_request, background_tasks)

    def add_method_route(
        self,
        func: FunctionType,
        *,
        name: str = None,
        **kwargs,
    ) -> None:
        name = name or func.__name__
        route = self.method_route_class(
            self,
            self.entrypoint_route.path + '/' + name,
            func,
            name=name,
            **kwargs,
        )
        self.routes.append(route)

    def method(
        self,
        **kwargs,
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.add_method_route(
                func,
                **kwargs,
            )
            return func

        return decorator


class API(FastAPI):
    def openapi(self):
        result = super().openapi()
        result['components']['schemas'].pop('ValidationError', None)
        result['components']['schemas'].pop('HTTPValidationError', None)
        list(result['paths'][k][k1]['responses'].pop('422', None)
             for k in result['paths'].keys() for k1 in result['paths'][k].keys())
        return result

    def bind_entrypoint(self, ep: Entrypoint):
        ep.bind_dependency_overrides_provider(self)
        self.routes.extend(ep.routes)
        self.on_event('shutdown')(ep.shutdown)


if __name__ == '__main__':
    import uvicorn

    app = API()

    api_v1 = Entrypoint('/api/v1/jsonrpc')


    class MyError(BaseError):
        CODE = 5000
        MESSAGE = 'My error'

        class DataModel(BaseModel):
            details: str


    @api_v1.method(errors=[MyError])
    def echo(
        data: str = Param(..., example='123'),
    ) -> str:
        if data == 'error':
            raise MyError(data={'details': 'error'})
        else:
            return data


    app.bind_entrypoint(api_v1)

    uvicorn.run(app, port=5000, debug=True, access_log=False)
