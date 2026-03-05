# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-03-05
### Added
- Initial release with multi-upstream MCP proxy.
- Routing precedence via path, header, in-band params, and default upstream.
- FastAPI HTTP interface with JSON and NDJSON support.
- Internal admin MCP interface for runtime config, health, and telemetry.
- Plugin system for upstream transports and telemetry sinks.
- Telemetry pipeline with bounded queue and retrying HTTP sink.
- Test suite for routing, auth, config application, plugin loading, telemetry, restart, and backpressure.
