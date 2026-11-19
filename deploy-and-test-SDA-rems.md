### Deploying SDA and REMS (non-starter versions)

1. Clone the git repo `https://github.com/WIFORCE/sda-deployment` including the `sensitive-data-archive` and `rems` submodules. 
2. There are several changes and optimisations made by the team. 
    - Both SDA and REMS can be deployed by the top level `docker-compose.yaml`.
    - Two override files `sensitive-data-archive.override.yml` and `rems.override.yml` change the configuration settings in our repository while using the latest code form the submodules. 
    - Directory `configuration` at the root of the repo contains all actual modifications. 
    - SDA version is fixed to `4.0.1-hotfix-2` using a `configuration/sensitive-data-archive/.env` file. 
    - This directory (`configuration`) also contains all clients, ga4gh visa broker and userinfo files. 
3. To bring up the working stack with only the essential containers run `docker compose up auth-aai mock-aai app -d`. 


### Changes and troubleshooting

1. We want the strack to use hostname instead of localhost. We changed localhost in all config files and also added localhost=hostname to the `.env` file specified above just to be safe. 
2. On deployment, we kept getting the error

```txt
State of incoming request (.....) does not match with your session's state ()
```

This was caused by SDA storeing the OIDC state in a cookie marked Secure (hard-coded, not configurable). Browsers accept Secure cookies over plain HTTP only on localhost. Once the stack moved to `http://pando.upsc.se:8801`, the browser dropped the cookie. The login callback then arrived without a stored state, which is the empty () in the log. Config changes alone couldn’t fix it, so `auth-aai` had to be served over HTTPS. To fix this we created certificates for the host and changed `auth-aai` config. 

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/auth.key -out certs/auth.crt \
  -subj "/CN=pando.upsc.se" -addext "subjectAltName=DNS:pando.upsc.se"
chmod 644 certs/auth.key

# auth-aai config
  auth-aai:
    environment:
      - AUTH_RESIGNJWT=false
      - DATABASE_PASSWORD=auth
      - DATABASE_USER=auth
      - SERVER_CERT=/certs/auth.crt
      - SERVER_KEY=/certs/auth.key
      - OIDC_REDIRECTURL=https://pando.upsc.se:8801/oidc/login
    volumes:
      - ./sda/config.yaml:/config.yaml
      - shared:/shared
      - ./certs:/certs:ro

# change http to https in configuration/aai-mock/clients/aai-auth.yaml
```

There is a way to set Secure on cookies only when reached over HTTPS as described here:
`https://github.com/neicnordic/sensitive-data-archive/commit/1635084f58fc60688a272641ccc8fc0fc45cf11b` and here `https://github.com/neicnordic/sensitive-data-archive/blob/v4.1.0/sda/cmd/auth/main.go`

3. To get REMS working the following changes were needed:

```bash
# changed public url in configuration/rems/config.edn 
{:port 3000
 :database-url "postgresql://db:5432/rems?user=rems&password=remspassword"
 :search-index-path "/tmp/rems-search-index"
 :public-url "http://pando.upsc.se:3000/" <---

# changed uris to include pando.upsc.se in rems-client.yaml

# In the rems.override.yml file
# moved the data volume to db so that the data persists if REMS is taken down
# Removed extra-hosts line
# added ports: !override - "3000:3000" to the app service. 
```
