import QtQuick
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

// GPU Monitor — live view of the AMD (sysfs) or NVIDIA (nvidia-smi) graphics
// card for the Omarchy bar.
//
// A thin surface: all live state comes from the shared davidjm.gpu service
// (bar.shell.serviceFor), which owns the one long-lived `gpumon.py` process
// and the state file it writes. One widget lives on each monitor's bar and
// they all render the selfsame `service.state` — see Service.qml.
//
// Left click: full details panel. Middle click: re-read now.
BarWidget {
  id: root

  moduleName: "davidjm.gpu"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // The readout turns red once the selected GPU temperature reaches this (°C).
  property string hotTemp: String(root.effective("hotTemp", "95"))
  readonly property var primaryTemperature: root.getPrimaryTemperature()

  // Live board state, published by the shared service instance.
  readonly property var svc: bar && bar.shell
    ? bar.shell.serviceFor("davidjm.gpu") : null
  readonly property var state: root.svc ? (root.svc.state || {}) : ({})

  property bool settingsOpen: false

  // ---------------------------------------------------------------- helpers

  function selfEntry() {
    var lc = root.bar ? root.bar.layoutConfig : null
    if (!lc) return null
    for (var r = 0; r < 3; r++) {
      var region = ["left", "center", "right"][r]
      var list = lc[region]
      if (!list) continue
      for (var i = 0; i < list.length; i++) {
        if (list[i] && (list[i].id || "") === "davidjm.gpu") return list[i]
      }
    }
    return null
  }

  function effective(name, fallback) {
    var v = root.setting(name, undefined)
    if (v !== undefined && v !== null) return v
    var entry = root.selfEntry()
    var e = entry ? entry[name] : undefined
    return (e === undefined || e === null) ? fallback : e
  }

  function pct(value) {
    if (value === null || value === undefined) return "-"
    var n = Number(value)
    if (!isFinite(n)) return "-"
    return Math.round(n) + "%"
  }

  function one(value, unit) {
    if (value === null || value === undefined) return "-"
    var n = Number(value)
    if (!isFinite(n)) return "-"
    return n + (unit !== undefined ? unit : "")
  }

  function tempC(value) {
    if (value === null || value === undefined) return "-°C"
    var n = Number(value)
    if (!isFinite(n)) return "-°C"
    return Math.round(n) + "°C"
  }

  function getPrimaryTemperature() {
    if (!root.state || root.state.present !== true) return null
    if (root.state.vendor === "AMD" && root.state.tempJunction !== null &&
        root.state.tempJunction !== undefined && isFinite(Number(root.state.tempJunction)))
      return { label: "Junction", value: Number(root.state.tempJunction) }
    if (root.state.vendor === "NVIDIA" && root.state.tempGpu !== null &&
        root.state.tempGpu !== undefined && isFinite(Number(root.state.tempGpu)))
      return { label: "GPU core", value: Number(root.state.tempGpu) }
    return null
  }

  function temperatureRows() {
    var rows = []
    if (root.state && Array.isArray(root.state.tempSensors)) {
      for (var i = 0; i < root.state.tempSensors.length && rows.length < 32; i++) {
        var sensor = root.state.tempSensors[i]
        if (!sensor || typeof sensor.label !== "string" || sensor.value === null || sensor.value === undefined) continue
        var value = Number(sensor.value)
        if (!isFinite(value)) continue
        rows.push({ label: sensor.label, value: root.tempC(value) })
      }
    }
    if (rows.length === 0 && root.state && root.state.present === true) {
      if (root.state.tempEdge !== null && root.state.tempEdge !== undefined)
        rows.push({ label: "Edge", value: root.tempC(root.state.tempEdge) })
      if (root.state.tempJunction !== null && root.state.tempJunction !== undefined)
        rows.push({ label: "Junction", value: root.tempC(root.state.tempJunction) })
      if (root.state.tempGpu !== null && root.state.tempGpu !== undefined)
        rows.push({ label: "GPU core", value: root.tempC(root.state.tempGpu) })
      if (root.state.tempMem !== null && root.state.tempMem !== undefined)
        rows.push({ label: "Memory", value: root.tempC(root.state.tempMem) })
    }
    return rows
  }

  function ghz(mhz) {
    if (mhz === null || mhz === undefined) return "-"
    var n = Number(mhz)
    if (!isFinite(n) || n <= 0) return "-"
    return (n >= 1000 ? (n / 1000).toFixed(2) + " GHz" : Math.round(n) + " MHz")
  }

  // Middle-click "re-read": ask the shared service to re-read its state now.
  function refreshState() {
    if (root.svc) root.svc.refresh()
  }

  function toggleSettings() {
    root.settingsOpen = !root.settingsOpen
  }
  function closeSettings() { root.settingsOpen = false }
  // KeyboardPanel funnels outside-click dismissal and popout handoff through
  // `owner`; both call these.
  function close() { root.closeSettings() }
  function closeForPopoutSwitch() { root.closeSettings() }

  // Persist the `hotTemp` key onto this widget's layout entry (the shell's
  // settings write, same hot-patch path every other widget uses).
  function saveHotTemp(n) {
    if (!root.bar || !root.bar.shell || typeof root.bar.shell.updateEntryInline !== "function") return
    var merged = {}
    var cur = root.settings || {}
    for (var k in cur) merged[k] = cur[k]
    merged.hotTemp = String(n)
    root.bar.shell.updateEntryInline(root.moduleName, merged)
  }

  // ------------------------------------------------------------------ colors

  property color base: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // --------------------------------------------------------------------- bar

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // \uEFC5 = fa-memory (Font Awesome 6 RAM stick) — reads as GPU/VRAM and
    // stays distinct from the CPU microchip (\uF2DB, used by cpu-clock).
    text: root.state && root.state.present === true
      ? "\uEFC5  " + root.pct(root.state.util) + "  " + (root.primaryTemperature
        ? root.tempC(root.primaryTemperature.value) : "GPU —")
      : "\uEFC5  GPU —"
    tooltipText: "GPU " + (root.state && root.state.present === true
      ? root.pct(root.state.util) + "·" + (root.primaryTemperature
        ? root.primaryTemperature.label + " " + root.tempC(root.primaryTemperature.value)
        : "temperature —")
        + (root.state.memUtil !== undefined && root.state.memUtil !== null ? " · mem " + root.pct(root.state.memUtil) : "")
        + (root.state.sclkMhz !== undefined && root.state.sclkMhz !== null ? " · sclk " + root.ghz(root.state.sclkMhz) : "")
        + (root.state.mclkMhz !== undefined && root.state.mclkMhz !== null ? " · mclk " + root.ghz(root.state.mclkMhz) : "")
        + (root.state.powerW !== undefined && root.state.powerW !== null ? " · " + root.one(root.state.powerW, " W") : "")
        + (root.state.fanRpm !== undefined && root.state.fanRpm !== null && root.state.fanRpm > 0 ? " · " + root.state.fanRpm + " rpm" : "")
        + (root.state.vramUsedG !== undefined && root.state.vramUsedG !== null ? " · VRAM " + root.one(root.state.vramUsedG, "G")
           + (root.state.vramTotalG ? "/" + root.one(root.state.vramTotalG, "G") : "") : "")
      : "not detected")
      + " — left panel · middle refresh"
    horizontalMargin: 6
    active: root.primaryTemperature !== null
            && root.primaryTemperature.value >= Number(root.hotTemp)

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) root.refreshState()
      else root.toggleSettings()
    }
  }

  // ------------------------------------------------------------ detail panel

  KeyboardPanel {
    id: settingsPanel
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.settingsOpen
    contentWidth: settingsPanel.fittedContentWidth(Style.space(300))
    contentHeight: settingsPanel.fittedContentHeight(form.implicitHeight)

    PanelKeyCatcher {
      anchors.fill: parent
      onCloseRequested: root.closeSettings()

      ColumnLayout {
        id: form
        anchors.fill: parent
        spacing: Style.space(8)

        Text {
          text: "GPU MONITOR"
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 2
        }

        Text {
          text: root.state && root.state.card ? root.state.card : "—"
          color: Qt.darker(Color.foreground, 1.4)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
          Layout.fillWidth: true
          Layout.topMargin: -4
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            text: "UTILIZATION"
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            Layout.alignment: Qt.AlignVCenter
          }
          Item { Layout.fillWidth: true }
          Text {
            text: root.pct(root.state.util)
            color: Color.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.heading
            font.bold: true
            Layout.alignment: Qt.AlignVCenter
          }
          Text {
            text: "MEM " + root.pct(root.state.memUtil)
            color: Qt.darker(Color.foreground, 1.4)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            text: "SCLK " + root.ghz(root.state.sclkMhz)
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignLeft
          }
          Text {
            text: "MCLK " + root.ghz(root.state.mclkMhz)
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignRight
          }
        }

        Text {
          text: "VRAM"
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          Layout.alignment: Qt.AlignLeft
          Layout.topMargin: Style.space(2)
        }

        Rectangle {
          Layout.fillWidth: true
          implicitHeight: 10
          radius: 5
          color: Qt.rgba(root.base.r, root.base.g, root.base.b, 0.07)
          border.color: Qt.rgba(root.base.r, root.base.g, root.base.b, 0.22)
          border.width: 1

          Rectangle {
            readonly property real frac: (root.state && root.state.vramTotalG
              ? Math.max(0, Math.min(1, root.state.vramUsedG / root.state.vramTotalG)) : 0)
            width: Math.round(parent.width * frac)
            height: parent.height
            radius: 5
            color: root.state && root.state.vramTotalG && frac > 0.85 ? Color.urgent : Color.accent
          }
        }

        Text {
          text: root.one(root.state.vramUsedG, " G")
            + (root.state.vramTotalG ? " / " + root.one(root.state.vramTotalG, " G") : "")
          color: Qt.darker(Color.foreground, 1.4)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignRight
        }

        RowLayout {
          spacing: Style.space(6)
          Layout.fillWidth: true
          Layout.topMargin: Style.space(2)

          Text {
            text: "POWER " + root.one(root.state.powerW, " W")
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignLeft
          }
          Text {
            text: root.state && root.state.fanRpm > 0 ? "FAN " + root.state.fanRpm + " rpm"
                                                      : "FAN —"
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignRight
          }
        }

        Text {
          text: "TEMPERATURES"
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 2
          Layout.topMargin: Style.space(2)
        }

        RowLayout {
          spacing: Style.space(6)
          Layout.fillWidth: true

          Repeater {
            model: root.temperatureRows()

            Rectangle {
              required property var modelData
              implicitWidth: Math.max(Style.space(52), chipText.implicitWidth + Style.space(10))
              implicitHeight: Style.space(24)
              radius: Style.cornerRadius
              color: Qt.rgba(root.base.r, root.base.g, root.base.b, 0.07)
              border.color: Qt.rgba(root.base.r, root.base.g, root.base.b, 0.22)
              border.width: 1

              Text {
                id: chipText
                anchors.centerIn: parent
                text: modelData.label + " " + modelData.value
                color: root.base
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }

        Text {
          text: "Hot temperature (turn red at/above)"
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignLeft
          Layout.topMargin: Style.space(2)
        }

        RowLayout {
          spacing: Style.space(6)
          Layout.fillWidth: true

          Repeater {
            model: ["85", "90", "95", "100", "105"]
            delegate: hotTempChip
          }
        }
      }
    }
  }

  // Hot-temperature threshold chip: applies + persists immediately.
  Component {
    id: hotTempChip

    Rectangle {
      required property string modelData
      property bool hot: false
      property bool checked: String(modelData) === String(root.hotTemp)
      implicitWidth: Math.max(Style.space(40), chipLabel.implicitWidth + Style.space(12))
      implicitHeight: Style.space(26)
      radius: Style.cornerRadius
      color: checked ? Qt.rgba(root.base.r, root.base.g, root.base.b, 0.16)
                     : (hot ? Qt.rgba(root.base.r, root.base.g, root.base.b, 0.09)
                            : Qt.rgba(root.base.r, root.base.g, root.base.b, 0.05))
      border.color: checked ? Qt.rgba(root.base.r, root.base.g, root.base.b, 0.55)
                            : Qt.rgba(root.base.r, root.base.g, root.base.b, 0.22)
      border.width: 1

      Text {
        id: chipLabel
        anchors.centerIn: parent
        text: modelData + "°"
        color: root.base
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: hot = true
        onExited: hot = false
        onClicked: root.saveHotTemp(modelData)
      }
    }
  }
}