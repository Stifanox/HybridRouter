"""Bramka hurtowni danych (ADR-014): protokół, strażnik kosztów, implementacje Google i atrapa.

Biblioteka ``google.cloud.bigquery`` jest importowana wyłącznie w
:mod:`app.warehouse.google_gateway` – pozostałe moduły aplikacji znają tylko protokół
:class:`~app.warehouse.gateway.WarehouseGateway` i wyjątki bramki.
"""
