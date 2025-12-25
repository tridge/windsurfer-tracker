#!/bin/bash

set -e

echo "Building Android phone app (sideload APK + playstore APK + playstore AAB)..."
pushd android
./gradlew assembleSideloadRelease assemblePlaystoreRelease bundlePlaystoreRelease
popd

echo ""
echo "Building Wear OS app..."
pushd wear
./gradlew assembleRelease bundleRelease
popd

echo ""
echo "Build complete:"
echo "  Android (sideload APK): android/app/build/outputs/apk/sideload/release/app-sideload-release.apk"
echo "  Android (playstore APK): android/app/build/outputs/apk/playstore/release/app-playstore-release.apk"
echo "  Android (playstore AAB): android/app/build/outputs/bundle/playstoreRelease/app-playstore-release.aab"
echo "  Wear OS (APK): wear/app/build/outputs/apk/release/app-release.apk"
echo "  Wear OS (AAB): wear/app/build/outputs/bundle/release/app-release.aab"
