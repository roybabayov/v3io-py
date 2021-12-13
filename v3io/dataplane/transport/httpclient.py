import ssl
import sys
import http.client
import socket

import v3io.dataplane.response
import v3io.dataplane.request
from . import abstract


class Transport(abstract.Transport):

    def __init__(self, logger, endpoint=None, max_connections=None, timeout=None, verbosity=None):
        super(Transport, self).__init__(logger, endpoint, max_connections, timeout, verbosity)

        # holds which connection index we'll use
        self._next_connection_idx = 0

        # based on scheme, create a host and context for _create_connection
        self._host, self._ssl_context = self._parse_endpoint(self._endpoint)

        # create the pool connection
        self._connections = self._create_connections(self.max_connections,
                                                     self._host,
                                                     self._ssl_context)

        # python 2 and 3 have different exceptions
        if sys.version_info[0] >= 3:
            self._wait_response_exceptions = (
                http.client.RemoteDisconnected, ConnectionResetError, ConnectionRefusedError, http.client.ResponseNotReady)
            self._send_request_exceptions = (
                BrokenPipeError, http.client.CannotSendRequest, http.client.RemoteDisconnected)
            self._get_status_and_headers = self._get_status_and_headers_py3
        else:
            self._wait_response_exceptions = (http.client.BadStatusLine, socket.error)
            self._send_request_exceptions = (http.client.CannotSendRequest, http.client.BadStatusLine)
            self._get_status_and_headers = self._get_status_and_headers_py2

    def requires_access_key(self):
        return True

    def restart(self):
        self.close()

        # recreate the connections
        self._connections = self._create_connections(self.max_connections,
                                                     self._host,
                                                     self._ssl_context)

    def close(self):
        for connection in self._connections:
            connection.close()

    def send_request(self, request, transport_state=None):
        if transport_state is None:
            connection_idx = self._get_next_connection_idx()
        else:
            connection_idx = transport_state.connection_idx

        # set the used connection on the request
        setattr(request.transport, 'connection_idx', connection_idx)

        # get a connection for the request and send it
        return self._send_request_on_connection(request, connection_idx)

    def wait_response(self, request, raise_for_status=None, num_retries=1):
        connection_idx = request.transport.connection_idx
        connection = self._connections[connection_idx]

        while True:
            try:

                # read the response
                response = connection.getresponse()
                response_body = response.read()

                status_code, headers = self._get_status_and_headers(response)

                self.log('Rx',
                         connection_idx=connection_idx,
                         status_code=status_code,
                         body=response_body)

                response = v3io.dataplane.response.Response(request.output,
                                                            status_code,
                                                            headers,
                                                            response_body)

                # enforce raise for status
                response.raise_for_status(request.raise_for_status or raise_for_status)

                # return the response
                return response

            except self._wait_response_exceptions as e:
                if num_retries == 0:
                    self._logger.warn_with('Remote disconnected while waiting for response and ran out of retries',
                                           e=type(e),
                                           connection_idx=connection_idx)

                    raise e

                self._logger.debug_with('Remote disconnected while waiting for response',
                                        retries_left=num_retries,
                                        connection_idx=connection_idx)

                num_retries -= 1

                # create a connection
                connection = self._recreate_connection_at_index(connection_idx)

                # re-send the request on the connection
                request = self._send_request_on_connection(request, connection_idx)
            except BaseException as e:
                self._logger.warn_with('Unhandled exception while waiting for response',
                                       e=type(e),
                                       connection_idx=connection_idx)
                raise e

    def _send_request_on_connection(self, request, connection_idx):
        path = request.encode_path()

        self.log('Tx',
                 connection_idx=connection_idx,
                 method=request.method,
                 path=path,
                 headers=request.headers,
                 body=request.body)

        connection = self._connections[connection_idx]

        try:
            connection.request(request.method, path, request.body, request.headers)
        except self._send_request_exceptions as e:
            self._logger.debug_with('Disconnected while attempting to send. Recreating connection', e=type(e))

            connection = self._recreate_connection_at_index(connection_idx)

            # re-request
            connection.request(request.method, path, request.body, request.headers)
        except BaseException as e:
            self._logger.warn_with('Unhandled exception while sending request', e=type(e))
            raise e

        return request

    def _recreate_connection_at_index(self, connection_idx):

        # close the old connection
        self._connections[connection_idx].close()

        # create a new one and replace
        connection = self._create_connection(self._host, self._ssl_context)
        self._connections[connection_idx] = connection

        return connection

    def _create_connections(self, num_connections, host, ssl_context):
        connections = []

        for connection_idx in range(num_connections):
            connection = self._create_connection(host, ssl_context)
            connection.connect()
            connections.append(connection)

        return connections

    def _create_connection(self, host, ssl_context):
        if ssl_context is None:
            return http.client.HTTPConnection(host)

        return http.client.HTTPSConnection(host, context=ssl_context)

    def _parse_endpoint(self, endpoint):
        if endpoint.startswith('http://'):
            return endpoint[len('http://'):], None

        if endpoint.startswith('https://'):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            return endpoint[len('https://'):], ssl_context

        return endpoint, None

    def _get_next_connection_idx(self):
        connection_idx = self._next_connection_idx

        self._next_connection_idx += 1
        if self._next_connection_idx >= len(self._connections):
            self._next_connection_idx = 0

        return connection_idx

    def _get_status_and_headers_py2(self, response):
        return response.status, response.getheaders()

    def _get_status_and_headers_py3(self, response):
        return response.code, response.headers
