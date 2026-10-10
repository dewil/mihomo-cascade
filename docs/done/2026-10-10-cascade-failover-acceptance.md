# CASC-FAILOVER: приемка

## Приемка10.10.2026

Независимый Haiku5.5 code review и полный combinedCI5622parallel/222678 +61serial/575 assertions GREEN без skips. Live proof: NUC→временный Xray26.3.27 VLESS/Reality на RU3→новый генератор330d654...→реальные DE/PL. PrimaryDE194.87.77.146, faultprimary→PL138.124.242.96 за23.1с, restore→DE29.1с; все-down29.2с блокируетpayload безDIRECT. В каждой доступной фазе CFстранаDE/PL/DE и OpenAIHTTP401. No reload/forced-delay; автоматические checks. Temporaryport53 positiveclosed(connectionrefused) сNUC, остаточныхRU3files0. Лог /tmp/cascade-failover-full-final.log. Это новыйгенераторtestchain, не утверждение о тогдашней опубликованнойprodцепочке.

Причина первоначального TLSблокера — Microsoftdecoy в этом стенде: замена толькоSNI/target наCloudflare позволила завершитьRealityTLS. Последующий fullNUC proof пройден. ProductionRealityconfigs не менялись. Harness e98156a20ace395ff3cd044aeeb2a25ae393092058beea418b5571561f17a9af; Haiku followupsourcehygiene gen-1791643560-kHJKf8rGzfzmi83VJ822, безновыхдефектовdecoychange. НизкийexistingriskSSHpublicfilecleanup отдельно проверен послеrun. Числовойalias0 producerсохраняет, runtimeотклоняетfailclosed, endtoendsupportнеобещан.
