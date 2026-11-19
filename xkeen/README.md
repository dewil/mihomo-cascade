# XKeen на Keenetic

Готовый `config.yaml` для [XKeen](https://github.com/jameszeroX/XKeen) с ядром Mihomo. Через подписку идет только YouTube, остальное напрямую. Группа `VPN` типа `fallback` сама переходит на следующую живую ноду, поэтому после смены нод в подписке ничего править не нужно. Сборщик каскада на роутер не ставится.

## Установка

1. Поставьте XKeen с ядром Mihomo (или Xray + Mihomo).
2. Сохраните копию `/opt/etc/mihomo/config.yaml`.
3. Замените его содержимым [config.yaml](config.yaml), в `url` впишите свою ссылку подписки.
4. Проверьте и перезапустите:

```sh
mihomo -t -d /opt/etc/mihomo -f /opt/etc/mihomo/config.yaml
xkeen -restart
```

То же можно сделать в веб-панели [XKeen-UI](https://github.com/zxc-rv/XKeen-UI): открыть `config.yaml` Mihomo, вставить, сохранить, применить.

5. В Keenetic: «Интернет» → «Приоритеты подключений» → «Политики доступа в интернет». У политики `xkeen` отметьте подключение к провайдеру, на вкладке «Применение политик» назначьте ее нужным устройствам.

## Проверка

С устройства под политикой `xkeen` откройте YouTube. В логе Mihomo строки `youtube.com` и `googlevideo.com` должны идти `using VPN[...]`, остальные сайты - `DIRECT`.

## Другие сайты через VPN

Добавьте строку перед `MATCH,DIRECT`, например `- DOMAIN-SUFFIX,chatgpt.com,VPN`, и перезапустите XKeen.
