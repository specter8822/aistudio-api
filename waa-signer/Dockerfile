FROM golang:1.27-bookworm AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils build-essential file && mkdir -p /opt/zig && curl -fsSL https://ziglang.org/download/0.16.0/zig-$(uname -m)-linux-0.16.0.tar.xz | tar -xJ --strip-components=1 -C /opt/zig && ln -s /opt/zig/zig /usr/local/bin/zig
COPY . .
RUN chmod +x build.sh && ./build.sh

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && apt-get clean
WORKDIR /app
COPY --from=builder /build/bin/waa-signer /app/waa-signer
RUN chmod +x /app/waa-signer
CMD ["/app/waa-signer"]
