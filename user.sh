# 1. log in as admin (your "storm" account) to get a token
TOKEN=$(curl -s -X POST http://127.0.0.1:8123/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"storm","password":"12961719","purpose":"operate"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 2. create the viewer
curl -s -X POST http://127.0.0.1:8123/auth/users \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"saulo2020","password":"saulo.mabel.2020","role":"viewer"}'
