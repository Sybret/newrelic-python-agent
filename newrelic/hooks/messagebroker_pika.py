import functools
import sys
import time
import types

from newrelic.api.application import application_instance
from newrelic.api.background_task import BackgroundTask
from newrelic.api.function_trace import FunctionTrace
from newrelic.api.amqp_trace import AmqpTrace
from newrelic.api.transaction import current_transaction
from newrelic.common.object_names import callable_name
from newrelic.common.object_wrapper import (wrap_function_wrapper, wrap_object,
        FunctionWrapper)


_no_trace_methods = set()
_START_KEY = '_nr_start_time'
KWARGS_ERROR = 'Supportability/hooks/pika/kwargs_error'


def _add_consume_rabbitmq_trace(transaction, method, properties,
        nr_start_time, subscribed=False):

    routing_key = None
    if hasattr(method, 'routing_key'):
        routing_key = method.routing_key

    # The transaction may have started after the message was received. In this
    # case, the start time is reset to the true transaction start time.
    transaction.start_time = min(nr_start_time,
            transaction.start_time)

    # create a trace starting at the time the message was received
    trace = AmqpTrace(transaction, library='RabbitMQ',
            operation='Consume', destination_name=method.exchange or 'Default',
            message_properties=properties,
            routing_key=routing_key,
            subscribed=subscribed)
    trace.__enter__()
    trace.start_time = nr_start_time
    trace.__exit__(None, None, None)


def _wrap_Channel_consume_callback(module, obj, bind_params,
        callback_referrer, subscribed=False):
    def _nr_wrapper_Channel_consume_(wrapped, instance, args, kwargs):

        transaction = current_transaction(active_only=False)
        callback = bind_params(*args, **kwargs)
        name = callable_name(callback)
        wrapped_callback = None

        # A consumer callback can be called either outside of a transaction, or
        # within the context of an existing transaction. There are 3
        # possibilities we need to handle: (Note that this is similar to our
        # Celery instrumentation)
        #
        #   1. In an inactive transaction
        #
        #      If the end_of_transaction() or ignore_transaction() API calls
        #      have been invoked, this task may be called in the context
        #      of an inactive transaction. In this case, don't wrap the task
        #      in any way. Just run the original function.
        #
        #   2. In an active transaction
        #
        #      Run the original function inside a FunctionTrace.
        #
        #   3. Outside of a transaction
        #
        #      Since it's not running inside of an existing transaction, we
        #      want to create a new background transaction for it but only when
        #      we've subscribed.

        if transaction and (transaction.ignore_transaction or
                transaction.stopped):
            # 1. In an inactive transaction
            return wrapped(*args, **kwargs)

        elif callback in _no_trace_methods:
            # This is an internal callback that should not be wrapped.
            return wrapped(*args, **kwargs)

        elif callback is None:
            return wrapped(*args, **kwargs)

        elif transaction:
            # 2. In an active transaction
            @functools.wraps(callback)
            def wrapped_callback(*args, **kwargs):

                # Keyword arguments are unknown since this is a user defined
                # callback
                if not kwargs:
                    method, properties = args[1:3]
                    start_time = (getattr(method, _START_KEY, None) or
                            getattr(wrapped_callback, _START_KEY, None))
                    _add_consume_rabbitmq_trace(transaction,
                            method,
                            properties and properties.__dict__,
                            start_time)
                else:
                    m = transaction._transaction_metrics.get(KWARGS_ERROR, 0)
                    transaction._transaction_metrics[KWARGS_ERROR] = m + 1
                with FunctionTrace(transaction=transaction, name=name):
                    return callback(*args, **kwargs)

        elif subscribed:
            # 3. Outside of a transaction
            @functools.wraps(callback)
            def wrapped_callback(*args, **kwargs):

                # Keyword arguments are unknown since this is a user
                # defined callback
                exchange = 'Unknown'
                if not kwargs:
                    method, properties = args[1:3]
                    exchange = method.exchange or 'Default'

                bt_group = 'Message/RabbitMQ/Exchange'
                bt_name = 'Named/%s' % exchange

                with BackgroundTask(application=application_instance(),
                        name=bt_name, group=bt_group) as bt:

                    # Keyword arguments are unknown since this is a user
                    # defined callback
                    if not kwargs:
                        start_time = (getattr(method, _START_KEY, None) or
                                getattr(wrapped_callback, _START_KEY, None))
                        _add_consume_rabbitmq_trace(bt,
                                method,
                                properties and properties.__dict__,
                                start_time,
                                subscribed=True)
                    else:
                        m = bt._transaction_metrics.get(KWARGS_ERROR, 0)
                        bt._transaction_metrics[KWARGS_ERROR] = m + 1
                    with FunctionTrace(transaction=bt, name=name):
                        return callback(*args, **kwargs)

        if wrapped_callback:
            if len(args) > 0:
                args = list(args)
                args[0] = wrapped_callback
            else:
                kwargs[callback_referrer] = wrapped_callback

            # This start time is used only for PULL style interactions with
            # RabbitMQ For example, BasicGet is a PULL style interaction. In
            # the BasicGet case, the segment measurement should include the
            # time from BasicGet to BasicGet.Ok.
            #
            # In the PUSH case (Basic.Deliver), the start time will be attached
            # to the method. The method based start time will override the
            # callback start time.
            wrapped_callback._nr_start_time = time.time()

        return wrapped(*args, **kwargs)

    wrap_function_wrapper(module, obj, _nr_wrapper_Channel_consume_)


def _bind_basic_publish(exchange, routing_key, body,
                    properties=None, mandatory=False, immediate=False):
    return (exchange, routing_key, body, properties, mandatory, immediate)


def _nr_wrapper_basic_publish(wrapped, instance, args, kwargs):
    transaction = current_transaction()

    if transaction is None:
        return wrapped(*args, **kwargs)

    from pika import BasicProperties

    (exchange, routing_key, body, properties, mandatory, immediate) = (
            _bind_basic_publish(*args, **kwargs))
    properties = properties or BasicProperties()
    properties.headers = properties.headers or {}
    cat_headers = AmqpTrace.generate_request_headers(transaction)
    for name, value in cat_headers:
        properties.headers[name] = value

    args = (exchange, routing_key, body, properties, mandatory, immediate)

    with AmqpTrace(transaction, library='RabbitMQ', operation='Produce',
            destination_name=exchange or 'Default',
            message_properties=properties.__dict__):
        return wrapped(*args)


def _nr_wrapper_Basic_Deliver_init_(wrapper, instance, args, kwargs):
    ret = wrapper(*args, **kwargs)
    instance._nr_start_time = time.time()
    return ret


def _nr_wrap_BlockingChannel___init__(wrapped, instance, args, kwargs):
    ret = wrapped(*args, **kwargs)
    # Add the bound method to the set of methods not to trace.
    _no_trace_methods.add(instance._on_consumer_message_delivery)
    return ret


def _consumer_callback_bind_params(consumer_callback, *args, **kwargs):
    return consumer_callback


def _callback_bind_params(callback=None, *args, **kwargs):
    return callback


def _ConsumeGeneratorWrapper(wrapped):
    def wrapper(wrapped, instance, args, kwargs):
        def _generator(generator):
            try:
                value = None
                exc = None

                while True:
                    transaction = current_transaction()

                    if exc is not None:
                        yielded = generator.throw(*exc)
                        exc = None
                    else:
                        yielded = generator.send(value)
                        if yielded:
                            method, properties, _ = yielded
                            nr_start_time = method._nr_start_time
                            _add_consume_rabbitmq_trace(transaction, method,
                                    properties, nr_start_time,
                                    subscribed=True)

                    try:
                        value = yield yielded
                    except Exception:
                        exc = sys.exc_info()

            finally:
                generator.close()

        try:
            result = wrapped(*args, **kwargs)
        except:
            raise
        else:
            if isinstance(result, types.GeneratorType):
                return _generator(result)
            else:
                return result

    return FunctionWrapper(wrapped, wrapper)


def instrument_pika_adapters(module):
    _wrap_Channel_consume_callback(module.blocking_connection,
            'BlockingChannel.basic_consume', _consumer_callback_bind_params,
            'consumer_callback', subscribed=True)
    wrap_function_wrapper(module.blocking_connection,
            'BlockingChannel.__init__', _nr_wrap_BlockingChannel___init__)
    wrap_object(module.blocking_connection, 'BlockingChannel.consume',
            _ConsumeGeneratorWrapper)


def instrument_pika_spec(module):
    wrap_function_wrapper(module.Basic.Deliver, '__init__',
            _nr_wrapper_Basic_Deliver_init_)


def instrument_pika_channel(module):
    wrap_function_wrapper(module, 'Channel.basic_publish',
            _nr_wrapper_basic_publish)

    _wrap_Channel_consume_callback(module, 'Channel.basic_consume',
            _consumer_callback_bind_params, 'consumer_callback',
            subscribed=True)
    _wrap_Channel_consume_callback(module, 'Channel.basic_get',
            _callback_bind_params, 'callback', subscribed=False)
