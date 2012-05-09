"""This module implements the communications layer with the data collector.

"""

import logging
import os
import socket
import sys
import zlib

try:
    import json
except:
    try:
        import simplejson as json
    except:
        import newrelic.lib.simplejson as json

import newrelic.lib.requests as requests

from newrelic import version
from newrelic.core.config import global_settings, create_settings_snapshot

_logger = logging.getLogger(__name__)

# User agent string that must be used in all requests. The data collector
# does not rely on this, but is used to target specific agents if there
# is a problem with data collector handling requests.

USER_AGENT = 'NewRelic-PythonAgent/%s (Python %s %s)' % (
         version, sys.version.split()[0], sys.platform)

# Internal exceptions that can be generated in network layer. These are
# use to control what the upper levels should do. Any actual details of
# errors would already be logged at the network level.

class NetworkInterfaceException(Exception): pass
class ForceAgentRestart(NetworkInterfaceException): pass
class ForceAgentDisconnect(NetworkInterfaceException): pass
class DiscardDataForRequest(NetworkInterfaceException): pass
class RetryDataForRequest(NetworkInterfaceException): pass
class ServerIsUnavailable(RetryDataForRequest): pass

# Data collector URL and proxy settings.

def collector_url(server=None):
    """Returns the URL for talking to the data collector. When no server
    'host:port' is specified then the main data collector host and port is
    taken from the agent configuration. When a server is explicitly passed
    it would be the secondary data collector which subsequents requests
    in an agent session should be sent to.

    """

    settings = global_settings()

    url = '%s://%s/agent_listener/invoke_raw_method'

    scheme = settings.ssl and 'https' or 'http'

    if not server:
        # When pulling port from agent configuration it should only be
        # set when testing against a local data collector. For staging
        # and production should not be set and would default to port 80
        # or 443 based on scheme name in URL and we don't explicitly
        # add the ports.

        if settings.port:
            server = '%s:%d' % (settings.host, settings.port)
        else:
            server = '%s' % settings.host

    return url % (scheme, server)

def proxy_server():
    """Returns the dictionary of proxy server settings to be supplied to
    the 'requests' library when making requests.
    
    """

    settings = global_settings()

    # Require that both proxy host and proxy port are set to work.

    if not settings.proxy_host or not settings.proxy_port:
        return

    # The agent configuration only provides means to set one proxy so we
    # assume that it will be set correctly depending on whether SSL
    # connection requested or not.

    scheme = settings.ssl and 'https' or 'http'
    proxy = '%s:%d' % (settings.proxy_host, settings.proxy_port)

    # Encode the proxy user name and password into the proxy server value
    # as requests library will strip it out of there and use that.

    if settings.proxy_user is not None and settings.proxy_pass is not None:
        proxy = '%s:%s@%s' % (settings.proxy_user, settings.proxy_pass, proxy)

    return { scheme: proxy }

# Low level network functions and session management. When connecting to
# the data collector it is initially done through the main data collector.
# It is though then necessary to ask the data collector for the per
# session data collector to use. Subsequent calls are then made to it.

def send_request(url, method, license_key, agent_run_id=None, payload=()):
    """Constructs and sends a request to the data collector."""

    params = {}
    headers = {}
    config = {}

    settings = global_settings()

    # Validate that the license key was actually set and if not replace
    # it with a string which makes it more obvious it was not set.

    if not license_key:
        license_key = 'NO LICENSE KEY WAS SET IN AGENT CONFIGURATION'

    # All our requests are fomatted for version 9 of the agent protocol.

    params['method'] = method
    params['license_key'] = license_key
    params['protocol_version'] = '9'
    params['marshal_format'] = 'json'

    if agent_run_id:
        params['run_id'] = str(agent_run_id)

    headers['User-Agent'] = USER_AGENT
    headers['Content-Encoding'] = 'identity'

    # The data collector doesn't appear to honour keep alive but set it
    # up in case that ever changes. Keep alive is handled within the
    # 'request' library automatically when it is supported by the server
    # using a pool of connections keyed on server name.

    config['keep_alive'] = True
    headers['Connection'] = 'Keep-Alive'

    # Set up definitions for proxy server in case that has been set.

    proxies = proxy_server()

    # At this time we use JSON content encoding for the data being
    # sent. Ensure that normal byte strings are interpreted as Latin-1
    # and that the final result is ASCII so that don't need to worry
    # about converting back to bytes again. We set the default fallback
    # encoder to treat any iterable as a list. Unfortunately the JSON
    # library can't use it as an iterable and so means that generator
    # will be consumed up front and everything collected in memory as a
    # list before then converting to JSON.
    #
    # If an error does occur when encoding the JSON, then it isn't
    # likely going to work later on in a subsequent request with same
    # data, even if aggregated with other data, so we need to log the
    # details and then flag that data should be thrown away. Don't mind
    # being noisy in the the log in this situation as it would indicate
    # a problem with the implementation of the agent.

    try:
        data = json.dumps(payload, ensure_ascii=True, encoding='Latin-1',
                default=lambda o: list(iter(o)))

    except Exception, exc:
        _logger.error('Error encoding data for JSON payload for method %r '
                'with payload of %r. Exception which occurred was %r. '
                'Please report this problem to New Relic support.' % (method,
                payload, exc))

        raise DiscardDataForRequest(str(exc))

    # Compress the serialized JSON being sent as content if over 64KiB
    # in size. If less than 2MB in size compress for speed. If over
    # 2MB then compress for smallest size. This parallels what the Ruby
    # agent does.

    if len(data) > 64*1024:
        headers['Content-Encoding'] = 'deflate'
        level = (len(data) < 2000000) and 1 or 9
        data = zlib.compress(data, level)

    # Send the request. We set 'verify' to be false so that when using
    # SSL there is no attempt to do SSL certificate validation. If it
    # were enabled then we would also need the 'certifi' library.
    #
    # The 'requests' library can raise a number of exception derived
    # from 'RequestException' before we even manage to get a connection
    # to the data collector.
    #
    # The data collector can the generate a number of different types of
    # HTTP errors for requests. These are:
    #
    # 400 Bad Request - For incorrect method type or incorrectly
    # construct parameters. We should not get this and if we do it would
    # likely indicate a problem with the implementation of the agent.
    #
    # 413 Request Entity Too Large - Where the request content was too
    # large. The limits on number of nodes in slow transaction traces
    # should in general prevent this, but not everything has size limits
    # and so rogue data could still blow things out. Same data is not
    # going to work later on in a subsequent request, even if aggregated
    # with other data, so we need to log the details and then flag that
    # data should be thrown away.
    #
    # 415 Unsupported Media Type - This occurs when the JSON which was
    # sent can't be decoded by the data collector. If this is a true
    # problem with the JSON formatting, then sending again, even if
    # aggregated with other data, may not work, so we need to log the
    # details and then flag that data should be thrown away.
    #
    # 503 Service Unavailable - This occurs when data collector, or core
    # application is being restarted and not in state to be able to
    # accept requests. It should be a transient issue so should be able
    # to retain data and try again.

    try:
        r = requests.post(url, params=params, headers=headers,
                config=config, proxies=proxies, verify=False, data=data)

    except requests.RequestException, exc:
        _logger.warning('Unable to connect to the data collector with '
                'url of %r. Error raised is %r.' % (url, exc))

        raise RetryDataForRequest(str(exc))

    if r.status_code != 200:
        _logger.debug('Received a non 200 HTTP response from the data '
                'collector where url=%r, method=%r, license_key=%r, '
                'agent_run_id=%r, params=%r, headers=%r, status_code=%r '
                'and content=%r.' % (url, method, license_key, agent_run_id,
                params, headers, r.status_code, r.content))

    if r.status_code == 400:
        _logger.error('Data collector is indicating that a bad '
                'request has been submitted for url %r, headers of %r, '
                'params of %r and payload of %r. Please report this '
                'problem to New Relic support.' % (url, headers, params,
                payload))

        raise DiscardDataForRequest()

    elif r.status_code == 413:
        _logger.warning('Data collector is indicating that a request for '
                'method %r was received where the request content size '
                'was over the maximum allowed size limit. The length of '
                'the request content was %d. If this keeps occurring on a '
                'regular basis, please report this problem to New Relic '
                'support for further investigation.' % (method, len(data)))

        raise DiscardDataForRequest()

    elif r.status_code == 415:
        _logger.warning('Data collector is indicating that it was sent '
                'malformed JSON data for method %r. If this keeps occurring '
                'on a regular basis, please report this problem to New '
                'Relic support for further investigation.' % method)

        if settings.debug.log_malformed_json_data:
            if headers['Content-Encoding'] == 'deflate':
                data = zlib.uncompress(data)

            _logger.debug('JSON data which was rejected by the data '
                    'collector was %r.' % data)

        raise DiscardDataForRequest(r.content)

    elif r.status_code == 503:
        _logger.warning('Data collector is unavailable. This can be a '
                'transient issue because of the data collector or our '
                'core application being restarted. If the issue persists '
                'it can also be indicative of a problem with our servers. '
                'In the event that availability of our servers is not '
                'restored after a period of time then please report this '
                'problem to New Relic support for further investigation.')

        raise ServerIsUnavailable()

    elif r.status_code != 200:
        _logger.warning('An unexpected HTTP response was received from the '
                'data collector of %r for method %r with payload of %r. If '
                'this issue persists then please report this problem to New '
                'Relic support for further investigation.' % (r.status_code,
                method, payload))

        raise DiscardDataForRequest()

    # If we got this far we should have a legitimate response from the
    # data collector. The response is JSON so need to decode it.
    # Everything will come back as Unicode. Make sure all strings are
    # decoded as 'UTF-8'.

    try:
        result = json.loads(r.content, encoding='UTF-8')

    except Exception, exc:
        _logger.error('Error decoding data for JSON payload for method %r '
                'with payload of %r. Exception which occurred was %r. '
                'Please report this problem to New Relic support.' % (method,
                r.content, exc))

        if settings.debug.log_malformed_json_data:
            _logger.debug('JSON data received from data collector which '
                    'could not be decoded was %r.' % r.content)

        raise DiscardDataForRequest(str(exc))

    # The decoded JSON can be either for a successful response or an
    # error. A successful response has a 'return_value' element and an
    # error an 'exception' element.

    if 'return_value' in result:
        return result['return_value']

    error_type = result['exception']['error_type']
    message = result['exception']['message']

    # Now need to check for server side exceptions. The following
    # exceptions can occur for abnormal events.

    _logger.debug('Received an exception from the data collector where '
            'url=%r, method=%r, license_key=%r, agent_run_id=%r, params=%r, '
            'headers=%r, error_type=%r and message=%r' % (url, method,
            license_key, agent_run_id, params, headers, error_type,
            message))

    if error_type == 'NewRelic::Agent::LicenseException':
        _logger.error('Data collector is indicating that an incorrect '
                'license key has been supplied by the agent. The value '
                'which was used by the agent is %r. Please correct any '
                'problem with the license key or report this problem to '
                'New Relic support.' % license_key)

        raise DiscardDataForRequest(message)

    elif error_type == 'NewRelic::Agent::PostTooBigException':
        _logger.warning('Core application is indicating that a request for '
                'method %r was received where the request content size '
                'was over the maximum allowed size limit. The length of '
                'the request content was %d. If this keeps occurring on a '
                'regular basis, please report this problem to New Relic '
                'support for further investigation.' % (method, len(data)))

        raise DiscardDataForRequest(message)

    # Server side exceptions are also used to inform the agent to
    # perform certain actions such as restart when server side
    # configuration has changed for this application or when agent is
    # being disabled remotely for some reason.

    if error_type == 'NewRelic::Agent::ForceRestartException':
        _logger.info('An automatic internal agent restart has been '
                'requested by the data collector for the application '
                'where the agent run was %r. The reason given for the '
                'forced restart is %r.' % (agent_run_id, message))

        raise ForceAgentRestart(message)

    elif error_type == 'NewRelic::Agent::ForceDisconnectException':
        _logger.alert('Disconnection of the agent has been requested by '
                'the data collector for the application where the '
                'agent run was %r. The reason given for the forced '
                'disconnection is %r. Please contact New Relic support '
                'for further information.' % (agent_run_id, message))

        raise ForceAgentDisconnect(message)

    # We received an unexpected server side error we don't know what
    # to do with.

    _logger.warning('An unexpected server error was received from the '
            'data collector for method %r with payload of %r. The error '
            'was of type %r with message %r. If this issue persists '
            'then please report this problem to New Relic support for '
            'further investigation.' % (method, payload, error_type, message))

    raise DiscardDataForRequest(message)

class ApplicationSession(object):

    """ Class which encapsulates communication with the data collector
    once the initial registration has been done.

    """

    def __init__(self, collector_url, license_key, configuration):
        self.collector_url = collector_url
        self.license_key = license_key
        self.configuration = configuration
        self.agent_run_id = configuration.agent_run_id

    def shutdown_session(self):
        """Called to perform orderly deregistration of agent run against
        data collector, rather than simply dropping the connection and
        relying on data collector to surmise that agent run is finished
        due to no more data being reported.

        """

        _logger.debug('Connecting to data collector to terminate session '
                'for agent run %r.' % self.agent_run_id)

        return send_request(self.collector_url, 'shutdown',
                self.license_key, self.agent_run_id)

    def send_metric_data(self, start_time, end_time, metric_data):
        """Called to submit metric data for specified period of time.
        Time values are seconds since UNIX epoch as returned by the
        time.time() function. The metric data should be iterable of
        specific metrics.

        """

        payload = (self.agent_run_id, start_time, end_time, metric_data)

        return send_request(self.collector_url, 'metric_data',
                self.license_key, self.agent_run_id, payload)

    def send_errors(self, errors):
        """Called to submit errors. The errors should be an iterable
        of individual errors details.

        NOTE Although the details for each error carries a timestamp,
        the data collector appears to ignore it and overrides it with
        the timestamp that the data is received by the data collector.

        """

        errors = list(errors)

        if not errors:
            return

        payload = (self.agent_run_id, errors)

        return send_request(self.collector_url, 'error_data',
                self.license_key, self.agent_run_id, payload)

    def send_transaction_traces(self, transaction_traces):
        """Called to submit transaction traces. The transaction traces
        should be an iterable of individual traces.

        NOTE Although multiple traces could be supplied, the agent is
        currently only reporting on the slowest transaction in the most
        recent period being reported on.

        """

        transaction_traces = list(transaction_traces)

        if not transaction_traces:
            return

        payload = (self.agent_run_id, transaction_traces)

        return send_request(self.collector_url, 'transaction_sample_data',
                self.license_key, self.agent_run_id, payload)

    def send_sql_traces(self, sql_traces):
        """Called to sub SQL traces. The SQL traces should be an
        iterable of individual SQL details.

        NOTE The agent currently only reports on the 10 slowest SQL
        queries in the most recent period being reported on.

        """

        sql_traces = list(sql_traces)

        if not sql_traces:
            return

        payload = (sql_traces,)

        return send_request(self.collector_url, 'sql_trace_data',
                self.license_key, self.agent_run_id, payload)

def create_session(license_key, app_name, linked_applications,
        environment, settings):

    """Registers the agent for the specified application with the data
    collector and retrieves the server side configuration. Returns a
    session object if successful through which subsequent calls to the
    data collector are made. If unsucessful then None is returned.
    
    """

    # If no license key provided in the call, fallback to using that
    # from the agent configuration file or environment variables. Flag
    # an error if the result still seems invalid.

    if not license_key:
        license_key = global_settings().license_key

    if not license_key:
        _logger.error('A valid account license key cannot be found. '
            'Has a license key be specified in the agent configuration '
            'file or via the NEW_RELIC_LICENSE_KEY environment variable?')

    try:
        # First need to ask the primary data collector which of the many
        # data collector instances we should use for this agent run.

        _logger.debug('Connecting to data collector to register agent with '
                'license_key=%r, app_name=%r, linked_applications=%r, '
                'environment=%r and settings=%r. ' % (license_key, app_name,
                linked_applications, environment, settings))

        url = collector_url()
        redirect_host = send_request(url, 'get_redirect_host', license_key)

        # Then we perform a connect to the actual data collector host
        # we need to use. All communications after this point should go
        # to the secondary data collector.

        app_names = [app_name] + linked_applications

        local_config = {}

        local_config['pid'] = os.getpid()
        local_config['language'] = 'python'
        local_config['host'] = socket.gethostname()
        local_config['app_name'] = app_names
        local_config['identifier'] = ','.join(app_names)
        local_config['agent_version'] = version
        local_config['environment'] = environment
        local_config['settings'] = settings

        payload = (local_config,)

        url = collector_url(redirect_host)
        server_config = send_request(url, 'connect', license_key,
                None, payload)

        # The agent configuration for the application in constructed
        # by taking a snapshot of the locally constructed configuration
        # and overlaying it with that from the server.

        application_config = create_settings_snapshot(server_config)

    except NetworkInterfaceException:
        # The reason for errors of this type have already been logged.
        # No matter what the error we just pass back None. The upper
        # layer needs to count how many success times this has failed
        # and escalate things with a more sever error.

        pass

    except:
        # Any other errors are going to be unexpected and likely will
        # indicate an issue with the implementation of the agent.

        _logger.exception('Unexpected exception when attempting to '
                'register the agent with the data collector. Please '
                'report this problem to New Relic support for further '
                'investigation.')

        pass

    else:
        # Everything fine so we create the session object through which
        # subsequent communication with data collector will be done.

        session = ApplicationSession(url, license_key, application_config)

        _logger.debug('Successfully registered agent with app_name=%r, '
                'redirect_host=%r and agent_run_id=%r.' % (app_name,
                redirect_host, session.agent_run_id))

        return session
