# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module providing handlers for URLs related to map/reduce and pipelines."""

__author__ = 'Mike Gainer (mgainer@google.com)'

import urllib

from mapreduce import main as mapreduce_main
from mapreduce import parameters as mapreduce_parameters

from common import safe_dom
from common.utils import Namespace
from controllers import utils
from models import custom_modules
from models.config import ConfigProperty

from google.appengine.api import users

# Module registration
custom_module = None
MODULE_NAME = 'Map/Reduce'
XSRF_ACTION_NAME = 'view-mapreduce-ui'

GCB_ENABLE_MAPREDUCE_DETAIL_ACCESS = ConfigProperty(
    'gcb_enable_mapreduce_detail_access', bool,
    safe_dom.NodeList().append(
        safe_dom.Element('p').add_text("""
Enables access to status pages showing details of progress for individual
map/reduce jobs as they run.  These pages can be used to cancel jobs or
sub-jobs.  This is a benefit if you have launched a huge job that is
consuming too many resources, but a hazard for naive users.""")
    ).append(
        safe_dom.Element('p').add_child(
        safe_dom.A('/mapreduce/ui/pipeline/list', target='_blank').add_text("""
See an example page (with this control enabled)"""))
    ), False, multiline=False, validator=None)


def authorization_wrapper(self, *args, **kwargs):
    # developers.google.com/appengine/docs/python/taskqueue/overview-push
    # promises that this header cannot be set by external callers.  If this
    # is present, we can be certain that the request is internal and from
    # the task queue worker.  (This is belt-and-suspenders with the admin
    # restriction on /mapreduce/worker*)
    if 'X-AppEngine-TaskName' not in self.request.headers:
        self.response.out.write('Forbidden')
        self.response.set_status(403)
        return
    self.real_dispatch(*args, **kwargs)


def ui_access_wrapper(self, *args, **kwargs):
    content_is_static = (
        self.request.path.startswith('/mapreduce/ui/') and
        (self.request.path.endswith('.css') or
         self.request.path.endswith('.js')))
    xsrf_token = self.request.get('xsrf_token')
    user_is_course_admin = utils.XsrfTokenManager.is_xsrf_token_valid(
        xsrf_token, XSRF_ACTION_NAME)
    ui_enabled = GCB_ENABLE_MAPREDUCE_DETAIL_ACCESS.value

    if ui_enabled and (content_is_static or
                       user_is_course_admin or
                       users.is_current_user_admin()):
        namespace = self.request.get('namespace')
        with Namespace(namespace):
            self.real_dispatch(*args, **kwargs)

        # Some places in the pipeline UI are good about passing the
        # URL's search string along to RPC calls back to Ajax RPCs,
        # which automatically picks up our extra namespace and xsrf
        # tokens.  However, some do not, and so we patch it
        # here, rather than trying to keep up-to-date with the library.
        params = {}
        if namespace:
            params['namespace'] = namespace
        if xsrf_token:
            params['xsrf_token'] = xsrf_token
        extra_url_params = urllib.urlencode(params)
        if self.request.path == '/mapreduce/ui/pipeline/status.js':
            self.response.body = self.response.body.replace(
                'rpc/tree?',
                'rpc/tree\' + window.location.search + \'&')

        elif self.request.path == '/mapreduce/ui/pipeline/rpc/tree':
            self.response.body = self.response.body.replace(
                '/mapreduce/worker/detail?',
                '/mapreduce/ui/detail?' + extra_url_params + '&')

        elif self.request.path == '/mapreduce/ui/detail':
            self.response.body = self.response.body.replace(
                'src="status.js"',
                'src="status.js?%s"' % extra_url_params)

        elif self.request.path == '/mapreduce/ui/status.js':
            replacement = (
                '\'namespace\': \'%s\', '
                '\'xsrf_token\': \'%s\', '
                '\'mapreduce_id\':' % (
                    namespace if namespace else '',
                    xsrf_token if xsrf_token else ''))
            self.response.charset = 'utf8'
            self.response.text = self.response.body.replace(
                '\'mapreduce_id\':', replacement)
    else:
        self.response.out.write('Forbidden')
        self.response.set_status(403)


def register_module():
    """Registers this module in the registry."""

    global_handlers = []
    for path, handler_class in mapreduce_main.create_handlers_map():
        # The mapreduce and pipeline libraries are pretty casual about
        # mixing up their UI support in with their functional paths.
        # Here, we separate things and give them different prefixes
        # so that the only-admin-access patterns we define in app.yaml
        # can be reasonably clean.
        if path.startswith('.*/pipeline'):
            if 'pipeline/rpc/' in path or path == '.*/pipeline(/.+)':
                path = path.replace('.*/pipeline', '/mapreduce/ui/pipeline')
            else:
                path = path.replace('.*/pipeline', '/mapreduce/worker/pipeline')
        else:
            if '_callback' in path:
                path = path.replace('.*', '/mapreduce/worker', 1)
            elif '/list_configs' in path:
                # This needs mapreduce.yaml, which we don't distribute.  Not
                # having this prevents part of the mapreduce UI front page
                # from loading, but we don't care, because we don't want
                # people using the M/R front page to relaunch jobs anyhow.
                continue
            else:
                path = path.replace('.*', '/mapreduce/ui', 1)

        # The UI needs to be guarded by a config so that casual users aren't
        # exposed to the internals, but advanced users can investigate issues.
        if '/ui/' in path or path.endswith('/ui'):
            if (hasattr(handler_class, 'dispatch') and
                not hasattr(handler_class, 'real_dispatch')):
                handler_class.real_dispatch = handler_class.dispatch
                handler_class.dispatch = ui_access_wrapper
            global_handlers.append((path, handler_class))

        # Wrap worker handlers with check that request really is coming
        # from task queue.
        else:
            if (hasattr(handler_class, 'dispatch') and
                not hasattr(handler_class, 'real_dispatch')):
                handler_class.real_dispatch = handler_class.dispatch
                handler_class.dispatch = authorization_wrapper
            global_handlers.append((path, handler_class))

    # Tell map/reduce internals that this is now the base path to use.
    mapreduce_parameters.config.BASE_PATH = '/mapreduce/worker'

    global custom_module
    custom_module = custom_modules.Module(
        MODULE_NAME,
        'Provides support for analysis jobs based on map/reduce',
        global_handlers, [])
    return custom_module
