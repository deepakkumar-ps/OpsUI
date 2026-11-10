cd ~/OpsUI

docker build -t deepakkumarps08122/opsui:1.4 .

docker images | grep opsui

docker logout
docker login -u deepakkumarps08122

docker push deepakkumarps08122/opsui:1.4

sed -i 's|deepakkumarps08122/opsui:1.3|deepakkumarps08122/opsui:1.4|g' opsui-stack.yml

grep -n "image:" opsui-stack.yml

docker stack deploy --with-registry-auth -c opsui-stack.yml opsui

docker service ps opsui_opsui --no-trunc
docker service ls | grep opsui
docker service logs --tail 100 opsui_opsui