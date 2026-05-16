#!/bin/bash

# если у вас в конфиге будут такие строки:
#
# DOMAIN-SUFFIX,api.ipify.org
# DOMAIN-SUFFIX,ipv4.icanhazip.com
# #DOMAIN-SUFFIX,ifconfig.me -- эта строка закоментирована
#

echo " "
echo "local ip:"
curl -4 -sS https://ifconfig.me
echo " "
echo " "
echo "tunnel ip:"
curl -4 -sS https://ipv4.icanhazip.com
curl -4 -sS https://api.ipify.org
echo " "
