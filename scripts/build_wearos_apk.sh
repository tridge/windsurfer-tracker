#!/bin/bash

set -e

echo "Building Wear OS app (sideload APK + playstore APK + playstore AAB)..."
pushd wear
./gradlew assembleSideloadRelease assemblePlaystoreRelease bundlePlaystoreRelease
popd

echo ""
echo "Build complete:"
echo "  Wear OS (sideload APK): wear/app/build/outputs/apk/sideload/release/app-sideload-release.apk"
echo "  Wear OS (playstore APK): wear/app/build/outputs/apk/playstore/release/app-playstore-release.apk"
echo "  Wear OS (playstore AAB): wear/app/build/outputs/bundle/playstoreRelease/app-playstore-release.aab"
ls -l wear/app/build/outputs/apk/sideload/release/app-sideload-release.apk
