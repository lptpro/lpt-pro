// SPDX-License-Identifier: GPL-3.0-or-later
// Video surface for the preview window. Qt Quick draws video on its own render thread,
// paced to the monitor's refresh, so main-window work can't delay frames.
import QtQuick
import QtMultimedia

Rectangle {
    id: root
    color: "black"
    // While a video plays, redraw on EVERY screen refresh (not only when a new video frame
    // arrives). Variable-refresh screens then see a steady rhythm instead of an uneven one.
    property bool steady: false

    VideoOutput {
        objectName: "out"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
    }

    // An invisible 1-pixel item kept animating = a frame drawn every refresh.
    Rectangle {
        width: 1; height: 1
        color: "black"
        opacity: 0.02
        NumberAnimation on rotation {
            from: 0; to: 360; duration: 1000
            loops: Animation.Infinite
            running: root.steady
        }
    }
}
