Introduction
---

This is a work in progress. This repository tries to deploy the SDA stack, all in one place.
We had a breaking change, instead of relying on the starter-kit repositories, we now depend on the original SDA repository.

Modifications applied by this repository
---
1. Add sensitive-data-archive
   1. Include the repository with `git submodule add ...`
   2. Created a docker-compose.yml that includes the sda compose configuration
   3. Created a configuration/sensitive-data-archive/.env that specifies the version of SDA to use

Diagram
---
[sda-s3-dependency-diagram.html](sda-s3-dependency-diagram.html)

Troubleshooting wiki
---
* 

