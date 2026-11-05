# Public SDK release

The version is defined in `hyperoptimizer/_version.py`. Update it before a release;
package metadata, `hyperoptimizer.__version__`, and the HTTP user agent use this value.

Configure a PyPI pending trusted publisher for project `hyperoptimizer`, GitHub owner `hyperoptimizer`, repository `python-sdk`, workflow `python-sdk.yml`, environment `pypi`. Confirm PyPI account ownership first, and create the matching GitHub environment. No publishing credential belongs in source.

Commit these changes and tag that exact commit `v0.1.0`. CI tests the package, validates distributions, installs the wheel outside the source tree, checks the tag and publishes using OIDC. Verify a clean public `pip install hyperoptimizer==0.1.0` before the examples release. Services migration remains gated on successful examples CI, release digests, and platform acceptance.
