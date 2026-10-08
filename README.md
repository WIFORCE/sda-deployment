Introduction
---

This is a work in progress. This repository tries to deploy the SDA stack, all in one place.
We had a breaking change, instead of relying on the starter-kit repositories, we now depend on the original SDA repository.

Modifications applied by this repository
---
1. Add the dependencies to SDA and REMS with `git submodule add ...`. Then, we include the submodule docker compose files in docker-compose.yml with overrides.
2. SDA
   * Created a configuration/sensitive-data-archive/.env that specifies the version of SDA to use
   * Created a configuration/aai-mock/clients/rems-client.yaml. The whole folder is added to the default configuration from the submodule with the service aai-mock-config-init.
3. REMS
   * Move the initialization of the database in a service instead of the imperative `docker-compose run --rm -e CMD="migrate" app`
   * Created a configuration/rems/config.edn
      * Changes the authentication from `fake` to `oidc`.
  * Resolve localhost to the host-gateway for the service app so it can reach lsaai-mock. 

Diagram
---
[sda-s3-dependency-diagram.html](sda-s3-dependency-diagram.html)

Troubleshooting wiki
---
* Compiling the SDA image
  * Should we run the build-all command?
  * Or should we replace the image parameter from PR${PR_NUMBER} to ${PR_NUMBER} so we can use "v4.0.2" ?

