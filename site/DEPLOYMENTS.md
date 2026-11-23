# Deployment contract

The current procedure is [Independent site releases and rollback](../docs/SITE_RELEASES.md).
It supersedes the September 3 combined-Worker instructions formerly at this path.

- Site assets: explicit protected `deploy-staged.yml` workflow.
- Backend/API: separate protected `deploy.yml` workflow.
- Railway producers: independent service release paths described in the
  [release matrix](../docs/RELEASE_BOUNDARIES.md) and [packaging guide](../deployment/python/README.md).

Do not run bare `wrangler deploy`: root `wrangler.toml` is retained for combined
restoration, not site publishing. A merge alone does not deploy Cloudflare.
Markdown under `site/` is not included in the separated public asset artifact;
read this pointer and the linked procedure in the repository.

The previous contract remains in Git history at cutover revision
`f759582e2b327989e31749ddfeae7df9c6474890`.
