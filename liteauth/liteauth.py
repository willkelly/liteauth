from Cookie import SimpleCookie
from urllib import quote, unquote
from time import gmtime, strftime, time
import datetime

try:
    import simplejson as json
except ImportError:
    import json

from swift.common.http import HTTP_CLIENT_CLOSED_REQUEST
from swift.common.swob import HTTPFound, Response, Request, HTTPUnauthorized, HTTPForbidden, HTTPNotFound, wsgify
from swift.common.utils import cache_from_env, get_logger, TRUE_VALUES, split_path
from swift.common.middleware.acl import clean_acl
from oauth import Client


def parse_lite_acl(acl_string):
    """
    Parses Litestack ACL string into an account list.

    :param acl_string: The standard Swift ACL string to parse.
    :returns: list of user accounts
    """
    accounts = []
    if acl_string:
        for value in acl_string.split(','):
            accounts.append(value)
    return accounts


class LiteAuth(object):

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.version = 'v1'
        self.google_client_id = conf.get('google_client_id')
        self.google_client_secret = conf.get('google_client_secret')
        self.service_domain = conf.get('service_domain')
        self.service_endpoint = conf.get('service_endpoint', 'https://' + self.service_domain)
        self.google_scope = conf.get('google_scope')
        self.google_auth = '/login/google/'
        self.shared_container = '/share/'
        self.shared_container_add = 'load'
        self.shared_container_remove = 'drop'
        self.google_prefix = 'g_'
        self.logger = get_logger(conf, log_route='lite-auth')
        self.log_headers = conf.get('log_headers', 'f').lower() in TRUE_VALUES
        self.system_accounts = conf.get('system_accounts', '').split()

    def extract_auth_token(self, env):
        auth_token = None
        try:
            auth_token = SimpleCookie(env.get('HTTP_COOKIE', ''))['session'].value
        except KeyError:
            pass
        return auth_token

    @wsgify
    def __call__(self, req):
        if req.path.startswith(self.google_auth):
            state = None
            if req.params:
                code = req.params.get('code')
                state = req.params.get('state')
                if code:
                    if not 'eventlet.posthooks' in req.environ:
                        req.bytes_transferred = '-'
                        req.client_disconnect = False
                        req.start_time = time()
                        response = self.do_google_login(req, code, state)
                        req.response = response
                        self.posthooklogger(req.environ, req)
                        return response
                    else:
                        return self.do_google_login(req, code, state)
            return self.do_google_oauth(state)
        token = self.extract_auth_token(req.environ)
        if token:
            req.headers['x-auth-token'] = token
            req.headers['x-storage-token'] = token
            user_data = self.get_cached_user_data(req.environ, token)
            if user_data:
                if req.path.startswith(self.shared_container):
                    path = req.path[(len(self.shared_container) - 1):]
                    try:
                        op, account, container = split_path(path, 1, 3, True)
                    except ValueError, e:
                        return HTTPNotFound(body=str(e))
                    if not op in [self.shared_container_add, self.shared_container_remove]:
                        return HTTPNotFound()
                    account_req = Request.blank('/%s/%s' % (self.version, user_data))
                    account_req.method = 'HEAD'
                    resp = account_req.get_response(self.app)
                    if resp.status_int >= 300:
                        return HTTPNotFound(body=resp.body)
                    shared = {}
                    if 'x-account-meta-shared' in resp.headers:
                        try:
                            shared = json.loads(resp.headers['x-account-meta-shared'])
                        except Exception:
                            pass
                    if self.shared_container_add in op:
                        shared['%s/%s' % (account, container)] = 'shared'
                    elif self.shared_container_remove in op:
                        del shared['%s/%s' % (account, container)]
                    account_req = Request.blank('/%s/%s' % (self.version, user_data))
                    account_req.method = 'POST'
                    self.copy_account_metadata(resp.headers, account_req.headers)
                    account_req.headers['x-account-meta-shared'] = json.dumps(shared)
                    return account_req.get_response(self.app)
                req.environ['REMOTE_USER'] = user_data
                req.headers['x-auth-token'] = '%s,%s' % (user_data, token)
            else:
                return HTTPUnauthorized()
        req.environ['swift.authorize'] = self.authorize
        req.environ['swift.clean_acl'] = clean_acl
        return self.app

    def do_google_oauth(self, state=None):
        c = Client(auth_endpoint='https://accounts.google.com/o/oauth2/auth',
            client_id=self.google_client_id,
            redirect_uri='%s%s' % (self.service_endpoint, self.google_auth))
        loc = c.auth_uri(scope=self.google_scope.split(','), access_type='offline', state=state)
        return HTTPFound(location=loc)

    def do_google_login(self, req, code, state=None):
        if 'eventlet.posthooks' in req.environ:
            req.bytes_transferred = '-'
            req.client_disconnect = False
            req.start_time = time()
            req.environ['eventlet.posthooks'].append(
                (self.posthooklogger, (req,), {}))
        if 'logout' in code:
            auth_token = self.extract_auth_token(req.environ)
            if auth_token:
                self.delete_user_data(req.environ, auth_token)
            cookie = self.create_session_cookie()
            resp = Response(request=req, status=302,
                headers={
                    'set-cookie': cookie,
                    'location': '%s%s?account=logout' % (self.service_endpoint, state)})
            req.response = resp
            return resp
        c = Client(token_endpoint='https://accounts.google.com/o/oauth2/token',
            resource_endpoint='https://www.googleapis.com/oauth2/v1',
            redirect_uri='%s%s' % (self.service_endpoint, self.google_auth),
            client_id=self.google_client_id,
            client_secret=self.google_client_secret)
        c.request_token(code=code)
        self.logger.info(c.__dict__)
        token = c.access_token
        if hasattr(c, 'refresh_token'):
            rc = Client(token_endpoint=c.token_endpoint,
                client_id=c.client_id,
                client_secret=c.client_secret,
                resource_endpoint=c.resource_endpoint)

            rc.request_token(grant_type='refresh_token',
                refresh_token=c.refresh_token)
            token = rc.access_token
            self.logger.info(rc.__dict__)
        if not token:
            req.response = HTTPUnauthorized()
            return req.response
        user_data = self.get_new_user_data(req.environ, c)
        if not user_data:
            req.response = HTTPForbidden()
            return req.response
        account_req = Request.blank('/%s/%s' % (self.version, user_data))
        account_req.method = 'HEAD'
        resp = account_req.get_response(self.app)
        if resp.status_int >= 300:
            req.response = HTTPNotFound()
            return req.response
        if not 'x-account-meta-userdata' in resp.headers:
            userdata_req = Request.blank('/%s/%s' % (self.version, user_data))
            userdata_req.method = 'POST'
            self.copy_account_metadata(resp.headers, userdata_req.headers)
            userdata_req.headers['x-account-meta-userdata'] = json.dumps(c.request('/userinfo'))
            userdata_req.get_response(self.app)
        cookie = self.create_session_cookie(token=token, expires_in=c.expires_in)
        resp = Response(request=req, status=302,
            headers={
                'x-auth-token': token,
                'x-storage-token': token,
                'x-storage-url': '%s/%s/%s' % (self.service_endpoint, self.version, user_data),
                'set-cookie': cookie,
                'location': '%s%s?account=%s' % (self.service_endpoint, state or '/', user_data)})
        #print resp.headers
        req.response = resp
        return resp

    def create_session_cookie(self, token='', path='/', expires_in=0):
        cookie = SimpleCookie()
        cookie['session'] = token
        cookie['session']['path'] = path
        if not self.service_domain.startswith('localhost'):
            cookie['session']['domain'] = self.service_domain
        expiration = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
        cookie['session']['expires'] = expiration.strftime('%a, %d %b %Y %H:%M:%S GMT')
        return cookie['session'].output(header='').strip()

    def delete_user_data(self, env, token):
        memcache_client = cache_from_env(env)
        if not memcache_client:
            raise Exception('Memcache required')
        memcache_token_key = '%s/token/%s' % (self.google_prefix, token)
        memcache_client.delete(memcache_token_key)

    def get_cached_user_data(self, env, token):
        user_data = None
        memcache_client = cache_from_env(env)
        if not memcache_client:
            raise Exception('Memcache required')
        memcache_token_key = '%s/token/%s' % (self.google_prefix, token)
        cached_auth_data = memcache_client.get(memcache_token_key)
        if cached_auth_data:
            expires, user_data = cached_auth_data
            if expires < time():
                user_data = None
        return user_data

    def get_new_user_data(self, env, client):
        user_data = client.request('/userinfo')
        if user_data:
            user_data = self.google_prefix + user_data.get('id')
            expires = time() + client.expires_in
            memcache_client = cache_from_env(env)
            memcache_token_key = '%s/token/%s' % (self.google_prefix, client.access_token)
            memcache_client.set(memcache_token_key, (expires, user_data),
                time=float(expires - time()))
        return user_data

    def authorize(self, req):
        try:
            version, account, container, obj = split_path(req.path, 1, 4, True)
        except ValueError:
            self.logger.increment('errors')
            return HTTPNotFound(request=req)
        if not account or not account.startswith(self.google_prefix):
            return self.denied_response(req)
        user_data = (req.remote_user or '')
        if req.method in 'POST' and 'x-zerovm-execute' in req.headers \
            and account in user_data:
                return None
        if account in user_data and\
           (req.method not in ('DELETE', 'PUT', 'POST') or container):
            req.environ['swift_owner'] = True
            return None
        if container:
            accounts = parse_lite_acl(getattr(req, 'acl', None))
            if '*' in accounts or user_data in accounts:
                return None
        return self.denied_response(req)

    def denied_response(self, req):
        if req.remote_user:
            self.logger.increment('forbidden')
            return HTTPForbidden(request=req)
        else:
            self.logger.increment('unauthorized')
            return HTTPUnauthorized(request=req)

    def posthooklogger(self, env, req):
        if not req.path.startswith(self.google_auth):
            return
        response = getattr(req, 'response', None)
        if not response:
            return
        trans_time = '%.4f' % (time() - req.start_time)
        the_request = quote(unquote(req.path))
        if req.query_string:
            the_request = the_request + '?' + req.query_string
            # remote user for zeus
        client = req.headers.get('x-cluster-client-ip')
        if not client and 'x-forwarded-for' in req.headers:
            # remote user for other lbs
            client = req.headers['x-forwarded-for'].split(',')[0].strip()
        logged_headers = None
        if self.log_headers:
            logged_headers = '\n'.join('%s: %s' % (k, v)
                for k, v in req.headers.items())
        status_int = response.status_int
        if getattr(req, 'client_disconnect', False) or\
           getattr(response, 'client_disconnect', False):
            status_int = HTTP_CLIENT_CLOSED_REQUEST
        self.logger.info(
            ' '.join(quote(str(x)) for x in (client or '-',
                     req.remote_addr or '-', strftime('%d/%b/%Y/%H/%M/%S', gmtime()),
                     req.method, the_request, req.environ['SERVER_PROTOCOL'],
                     status_int, req.referer or '-', req.user_agent or '-',
                     req.headers.get('x-auth-token',
                         req.headers.get('x-auth-admin-user', '-')),
                     getattr(req, 'bytes_transferred', 0) or '-',
                     getattr(response, 'bytes_transferred', 0) or '-',
                     req.headers.get('etag', '-'),
                     req.environ.get('swift.trans_id', '-'), logged_headers or '-',
                     trans_time)))

    def copy_account_metadata(self, src_headers, dst_headers):
        prefix = 'x-account-meta-'
        for k, v in src_headers.iteritems():
            if k.startswith(prefix):
                dst_headers[k] = v


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)

    def auth_filter(app):
        return LiteAuth(app, conf)
    return auth_filter