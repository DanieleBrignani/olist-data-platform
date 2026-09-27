# Grafana with its Prometheus data-source plugin baked in at BUILD time.
#
# Grafana 13 ships data sources as external plugins that a background installer downloads
# from grafana.com at every start and auto-updates to the latest version. On a slow network
# the dashboard showed nothing until the download finished, and the plugin version floated
# (docs/what-failed.md). Baking a pinned version in makes startup offline and deterministic.
FROM grafana/grafana:13.2.2

ENV GF_PATHS_PLUGINS=/opt/grafana-plugins
USER root
RUN mkdir -p /opt/grafana-plugins \
 && grafana cli --pluginsDir /opt/grafana-plugins plugins install prometheus 13.2.1 \
 && chown -R 472:0 /opt/grafana-plugins
USER 472
