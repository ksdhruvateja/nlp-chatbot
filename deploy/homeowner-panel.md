# Replace Fixera in the FixBridge homeowner panel

The website source and its session contract are not included in this service.
The following is a supported integration boundary, not a claim that the live
website has already been changed. Keep the existing panel navigation and
replace the Fixera content component with the assistant.

## Same-origin route

Serve this service behind the site's existing HTTPS gateway under a dedicated
prefix, for example `/homeowner/repair-assistant/`. Redirect the prefix without
its trailing slash to the slash URL. Forward the prefix to this service,
stripping it from the upstream path, and set
`ROOT_PATH=/homeowner/repair-assistant`. Static files and API calls use relative
URLs, so they stay under that prefix instead of colliding with the website's
`/api` routes. The existing website must exclude this prefix from its SPA
fallback. Do not route the entire homeowner panel to this service.

Use an iframe with `src="/homeowner/repair-assistant/?embed=1"` inside the
homeowner panel. Embedded mode hides the standalone header and developer
login, and keeps its bearer token in memory rather than localStorage.

The parent and frame must share an origin. After the frame signals readiness,
the panel obtains a short-lived **service token from its trusted backend** and
sends it to the frame:

```javascript
const frame = document.querySelector('#repair-assistant');
window.addEventListener('message', async (event) => {
  if (event.origin !== location.origin || event.source !== frame.contentWindow ||
      event.data?.type !== 'fixbridge:ready') return;
  // Implement this endpoint in the existing authenticated website backend.
  // It must validate the homeowner's session; it must never accept a user ID
  // from the browser as proof of identity.
  const response = await fetch('/YOUR_BACKEND/service-token', {method: 'POST'});
  if (!response.ok) return;
  const {token} = await response.json();
  frame.contentWindow.postMessage({type: 'fixbridge:authenticate', token}, location.origin);
});
// On website logout:
// frame.contentWindow.postMessage({type: 'fixbridge:logout'}, location.origin);
```

This service accepts its HMAC tokens (`app.security.issue_token`). The website
backend must map the authenticated homeowner to the corresponding service user
and mint a short-lived token using the shared signing secret, or implement the
website's real session/JWT verifier in `current_user`. Do not send signing
secrets to the browser or put bearer tokens in URLs. Tokens must be renewed by
the host before expiry (send the authenticate message again).

Provision the homeowner's properties, memberships and equipment with the same
identity mapping. The service always checks property ownership/membership;
posting a property ID or embedding the UI does not grant access. Review the
existing website schema before sharing a database or mapping IDs.

## Deployment settings

Keep `DEV_AUTH=false`; set strong `FIXBRIDGE_SECRET` and
`MEDIA_ENCRYPTION_KEY`. Configure the real AI provider and licensed knowledge.
Use private S3 for media with multiple API/worker tasks, with the same media
encryption key across tasks. See `aws/README.md`. Keep authentication and
booking integration work separate from the offline demo credentials/content.

## Acceptance checks in the real panel

Verify logged-in homeowners see only their own properties and requests, another
homeowner cannot fetch their requests/media, logout clears the frame session,
and assets/API/media work under the configured prefix. Submit a repair with an
upload, answer a clarification, acknowledge/cancel safety warnings and record
an outcome. Recheck the site's other API routes and navigation. These checks
require the website source, its authenticated test environment and test users.
