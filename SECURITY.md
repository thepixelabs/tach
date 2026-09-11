# Security

Tach runs on your machine and reads files your coding agents already wrote. It
has no account, no server component, and sends nothing anywhere. This page says
what that does and does not protect you from, and how to report a problem.

## Reporting a vulnerability

Open a [security advisory](https://github.com/thepixelabs/tach/security/advisories/new),
or a normal issue if you would rather discuss it in the open. There is no bounty
and no formal SLA. It is one person and a weekend project, so expect a reply in
days rather than hours.

Please do not include real transcript content in a report. A minimal reproduction
is more useful and does not hand your own history to everyone reading the issue.

## What Tach does

- Binds to `127.0.0.1` only. It is never reachable from your network.
- Opens every database read only, and never writes to your agent history.
- Makes outbound requests only to local inference servers on loopback.
- Sends a `Content-Security-Policy` that stops the page loading third party
  code or posting anything off the machine.
- Rejects requests whose `Host` header is not a local name, which is what stops
  a remote site pointing its DNS at `127.0.0.1` and treating your machine as
  its own origin.
- Serves no cross origin headers, so a page you visit cannot read the API.

## What it does not protect you from

- **Anything already running as you.** A local process can read the same files
  Tach reads. Tach is not a sandbox.
- **Your own screenshots.** Paths are masked to `~/...` where they are rendered
  as paths, but project names, session titles, branch names and model names are
  shown as they are, and message bodies are shown verbatim. Check the frame
  before you post a screenshot.
- **Anyone you give the port to.** If you forward `3344` or run it behind a
  proxy, everything above stops being true. Do not.

## Scope

In scope: anything that lets a web page, another machine, or crafted transcript
content read your data or execute code in the dashboard.

Out of scope: attacks that require an attacker already having code execution or
a shell on your machine, and denial of service against your own single user
dashboard.

## Dependencies

The backend is the Python standard library, nothing else. The frontend vendors
three files, each with its licence retained in the source: `marked` (MIT) for
markdown, Lucide (ISC) and Simple Icons (CC0) for icons, plus three OFL fonts
under `static/assets/fonts/`. There is no package manager and no lockfile, so
there is no transitive dependency tree to audit.
