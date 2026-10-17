# Containerfile: the Android Appliance image (docs/ARCHITECTURE.md, "Image").
#
#   podman build --build-arg API_LEVEL=36 --build-arg VERSION="$(cat VERSION)" \
#     -t localhost/android-appliance:"$(cat VERSION)"-api36 .
#
# The build downloads the pinned SDK archives of conf/sdk.lock. The
# Android SDK License applies to them (https://developer.android.com/studio/terms),
# and it does not allow you to publish the image.
#
# Build arguments: API_LEVEL (default 36), VERSION, and SDK_REPOSITORY,
# SDK_EMULATOR_VERSION, SDK_PLATFORM_TOOLS_VERSION, SDK_IMAGE_REVISION
# (see scripts/fetch-sdk.sh; empty selects the lock default).

ARG API_LEVEL=36

# The runtime of every stage that runs Python, and of the final image.
# ldd finds most emulator libraries. The emulator loads libGL, libEGL,
# libGLESv2, libX11, libX11-xcb, libvulkan, libfreetype and libpulse with
# dlopen, so ldd does not show them. Xvnc is the GLX server of the GPU
# mode, glxinfo checks its renderer, and ip finds the addresses for coturn.
FROM registry.fedoraproject.org/fedora:44 AS runtime
RUN dnf -y --setopt=install_weak_deps=False install \
        libpng libxcb expat dbus-libs libX11 libX11-xcb libdrm libxkbfile \
        nss nss-util nspr libXi libXext libSM libICE pulseaudio-libs libbsd \
        libtiff freetype \
        mesa-dri-drivers mesa-libEGL mesa-libGL mesa-libgbm mesa-vulkan-drivers \
        vulkan-loader libglvnd-egl libglvnd-gles libglvnd-glx libglvnd-opengl \
        tigervnc-x11-server glx-utils coturn python3 iproute procps-ng \
    && dnf clean all \
    && rm -rf /var/cache/dnf /var/cache/libdnf5 /var/log/dnf5.log*
# Fedora 44 has no libtiff.so.5. Only a Qt image plugin links it, and a
# headless emulator never loads that plugin. Its symbols are all
# LIBTIFF_4.0, which libtiff.so.6 still exports.
RUN ln -s libtiff.so.6 /usr/lib64/libtiff.so.5 \
    && mkdir -m 0755 /data /run/appliance

# The SDK has a stage of its own, so a change of the runtime packages
# does not download it again. The tools and the system image are two
# steps, so images for two API levels share the tools.
FROM registry.fedoraproject.org/fedora:44 AS sdk
RUN dnf -y --setopt=install_weak_deps=False install unzip && dnf clean all
COPY conf/sdk.lock /build/conf/sdk.lock
COPY scripts/fetch-sdk.sh /build/scripts/fetch-sdk.sh
ARG SDK_REPOSITORY=
ARG SDK_EMULATOR_VERSION=
ARG SDK_PLATFORM_TOOLS_VERSION=
RUN bash /build/scripts/fetch-sdk.sh --sdk /sdk/tools tools
ARG API_LEVEL
ARG SDK_IMAGE_REVISION=
RUN bash /build/scripts/fetch-sdk.sh --sdk /sdk/image image

# The virtual environment of the daemon, and the gRPC stubs. protoc runs
# from a throwaway environment. The bytecode is compiled here because
# the root file system is read-only at run time. Hash-based bytecode
# stays valid whatever the file times after the copy.
FROM runtime AS python
RUN dnf -y --setopt=install_weak_deps=False install python3-pip && dnf clean all
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY requirements.lock /build/requirements.lock
RUN python3 -m venv --without-pip /opt/appliance/venv \
    && python3 -m pip --python /opt/appliance/venv/bin/python install \
        --require-hashes --no-deps -r /build/requirements.lock \
    && site=$(/opt/appliance/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') \
    && echo /opt/appliance >"$site/appliance.pth" \
    && /opt/appliance/venv/bin/python -m compileall -q -f -j 0 \
        --invalidation-mode checked-hash /opt/appliance/venv
COPY requirements-dev.lock /build/requirements-dev.lock
RUN python3 -m venv --without-pip /build/protoc \
    && python3 -m pip --python /build/protoc/bin/python install \
        --require-hashes --no-deps -r /build/requirements-dev.lock
COPY scripts/gen-protos.sh /build/scripts/gen-protos.sh
COPY appliance/proto/ /build/appliance/proto/
RUN bash /build/scripts/gen-protos.sh /build/protoc/bin/python /build/_proto

# The appliance package with the stubs, androidctl, the dashboard when
# there is one, and the version. The files of the context can be private
# to their owner, but the appliance runs as the user of keep-id.
FROM runtime AS app
COPY . /build/
COPY --from=python /build/_proto/ /build/_proto/
RUN mkdir -p /opt/appliance/appliance/_proto \
    && cp -a /build/VERSION /build/appliance /build/bin /opt/appliance/ \
    && if [ -d /build/web ]; then cp -a /build/web /opt/appliance/; fi \
    && cp -a /build/_proto/. /opt/appliance/appliance/_proto/ \
    && python3 -m compileall -q -j 0 --invalidation-mode checked-hash /opt/appliance/appliance \
    && chmod -R a+rX,go-w /opt/appliance

FROM runtime
COPY --from=sdk /sdk/tools/ /opt/android-sdk/
COPY --from=sdk /sdk/image/ /opt/android-sdk/
# With -grpc-use-token the emulator also checks its gRPC allow list, and the
# list has no entry for the WebRTC service (Rtc): RequestRtcStream is refused
# ("is not on the allowlist"). Allow the service next to EmulatorController.
# The token still guards every call.
RUN f=/opt/android-sdk/emulator/lib/emulator_access.json \
    && sed -i 's|^\( *\)"/android.emulation.control.EmulatorController/.\*",$|&\n\1"/android.emulation.control.v2.Rtc/.*",|' "$f" \
    && grep -q 'android.emulation.control.v2.Rtc/' "$f"
COPY --from=python /opt/appliance/venv/ /opt/appliance/venv/
COPY --from=app /opt/appliance/ /opt/appliance/
ENV PATH=/opt/appliance/venv/bin:/opt/appliance/bin:/opt/android-sdk/emulator:/opt/android-sdk/platform-tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    HOME=/data/home \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The url and vendor labels replace those of the Fedora base image.
ARG API_LEVEL
ARG VERSION=unknown
LABEL org.opencontainers.image.title="Android Appliance" \
      org.opencontainers.image.description="Headless Android emulator with a control API, a dashboard and an MCP server" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.source="https://github.com/JuanDelPueblo/android-appliance" \
      org.opencontainers.image.url="https://github.com/JuanDelPueblo/android-appliance" \
      org.opencontainers.image.vendor="" \
      android-appliance.api-level="$API_LEVEL"
EXPOSE 8080/tcp 3478/udp 3478/tcp
CMD ["/opt/appliance/venv/bin/python", "-m", "appliance", "serve"]
