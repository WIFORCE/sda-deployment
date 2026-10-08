### Deploying SDA stack from the production repo using Makefile

We have reached a conclusion that setting up SDA using the production version of SDA is the way to go forward. The production version of SDA has already built in authentication to test. Will do all steps on pando. 
So far:

1. Cloned SDA repo in /home/admin/Git
2. SDA needs GO version > 1.25. To install:

    ```bash
    wget https://go.dev/dl/go1.27.1.linux-amd64.tar.gz
    tar -C /usr/local -xzf go1.27.1.linux-amd64.tar.gz
    GOROOT=/usr/local/go 
    PATH=$PATH:$GOROOT/bin:$GOPATH/bin
    update-alternatives --install "/usr/bin/go" "go" "/usr/local/go/bin/go" 0
    update-alternatives --set go /usr/local/go/bin/go
    go version
    ```
3. After that followed instructions from the README.md and ran 

    ```bash
    make build-all
    make integrationtest-sda-s3-run
    ```
4. The test failed when running the `/tests/sda/09_healthchecks.sh` script. 
    
    ```txt
    Executing test script /tests/sda/09_healthchecks.sh
    make: *** [Makefile:124: integrationtest-sda-s3-run] Error 7
    ```
5. The `09_healthchecks.sh` script curls `http://s3inbox:8000` and `http://s3inbox:8000/health`. So checked the logs for the s3inbox container using `docker compose -f .github/integration/sda-s3-integration.yml logs s3inbox` to find

    ```txt
    s3inbox  | time="2026-10-02T12:22:25Z" level=fatal msg="failed to read jwt pub key from url: http://localhost:8800/oidc/jwk, due to jwk.Fetch failed (failed to fetch \"http://localhost:8800/oidc/jwk\": Get \"http://localhost:8800/oidc/jwk\": dial tcp [::1]:8800: connect: connection refused) for http://localhost:8800/oidc/jwk"
    ```
6. Turns out it was a UFW issue. We disabled the firewall and the integration set up worked. 
7. To keep the firewall and still allow the stack to deploy add them to the UFW rule. P.S: In the past deploying Rucio on `pando` had broken NFS connection to it. I had then modified the IP ranges assigned to docker in `/etc/docker/daemon.json` to 10.98.0.0/24. First instinct was to add these to the firewall rules but that did not work. Then checked what IPs are used by the network created by the stack. Turns out it was 10.100.0.0/24. Added this to the UFW rule and now it works. The commands are:

    ```bash
    docker network inspect <integration_default> # integration_default is the name of the network thatis created by the test
    sudo ufw allow from 10.100.0.1/24
    sudo ufw route allow from 10.100.0.1/24 
    sudo ufw reload
    sudo systemctl restart docker
    ```
