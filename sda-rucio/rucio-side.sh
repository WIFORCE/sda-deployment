docker exec -i dev-rucio-1 bash <<'EOF'
set -e
rucio scope add sda --account root
rucio rse add SDA_ARCHIVE --non-deterministic
rucio rse protocol add SDA_ARCHIVE --hostname sda-s3 --scheme https --port 443 --prefix / \
  --impl rucio.rse.protocols.gfal.NoRename \
  --domain-json '{"wan":{"read":1,"write":1,"delete":null,"third_party_copy_read":1,"third_party_copy_write":null},"lan":{"read":1,"write":1,"delete":null}}'
for kv in sign_url=s3 skip_upload_stat=True verify_checksum=False strict_copy=True s3_url_style=path; do
  rucio rse attribute set SDA_ARCHIVE --key "${kv%%=*}" --value "${kv#*=}"
done
rucio rse update SDA_ARCHIVE --key availability_delete --value False
rucio account limit set root --rse SDA_ARCHIVE --bytes infinity
rucio account attribute set root --key sign-url --value true
RSE_ID=$(python3 -c "from rucio.client import Client; print(Client().get_rse('SDA_ARCHIVE')['id'])")
echo "{\"$RSE_ID\": {\"access_key\": \"rucioread\", \"secret_key\": \"rucioreadsecret\", \"signature_version\": \"s3v4\", \"region\": \"us-east-1\"}}" \
  > /opt/rucio/etc/rse-accounts.cfg
EOF
