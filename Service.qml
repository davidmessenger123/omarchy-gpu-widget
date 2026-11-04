import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  property var state: ({})
  property var gpuProcesses: []
  property bool detailOpen: false
  property string lastStateText: ""
  property int monitorFailures: 0

  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
    return path.charAt(path.length - 1) === "/" ? path : path + "/"
  }
  readonly property string runtimeDir: {
    var value = ""
    if (typeof Quickshell.env === "function") {
      var environmentValue = Quickshell.env("XDG_RUNTIME_DIR")
      if (environmentValue !== null && environmentValue !== undefined)
        value = String(environmentValue)
    }
    value = value.replace(/\/+$/, "")
    if (value.charAt(0) !== "/" || /[\x00-\x1f]/.test(value)) return ""
    return value
  }
  readonly property string stateDir: root.runtimeDir === ""
    ? "" : root.runtimeDir + "/omarchy-gpu-widget"
  readonly property string statePath: root.stateDir === ""
    ? "" : root.stateDir + "/davidjm-gpu-state.json"
  readonly property var processEnvironment: ({
    "XDG_RUNTIME_DIR": root.runtimeDir,
    "PATH": "/usr/bin:/bin"
  })

  Process {
    id: monitorProc
    command: root.statePath === "" ? [] : [
      "/usr/bin/python3", root.pluginDir + "gpumon.py",
      "--state-file", root.statePath
    ]
    clearEnvironment: true
    environment: root.processEnvironment
    running: root.statePath !== ""
    onExited: {
      if (root.statePath === "") return
      root.monitorFailures = Math.min(root.monitorFailures + 1, 5)
      monitorRestart.interval = Math.min(3000 * Math.pow(2, root.monitorFailures - 1), 60000)
      monitorRestart.restart()
    }
  }

  Timer {
    id: monitorRestart
    interval: 3000
    onTriggered: if (root.statePath !== "") monitorProc.running = true
  }
  Timer {
    interval: 5000
    repeat: false
    running: monitorProc.running
    onTriggered: root.monitorFailures = 0
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: root.statePath !== ""
    printErrors: false
    onFileChanged: reload()
    onTextChanged: {
      var text = stateFile.text()
      if (text === root.lastStateText) return
      root.lastStateText = text
      root.parseState(text)
    }
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.statePath !== ""
    onTriggered: if (root.statePath !== "") stateFile.reload()
  }

  function numberOrNull(value, minimum, maximum) {
    if (value === null || value === undefined) return null
    if (typeof value !== "number" || !isFinite(value)) return undefined
    if (value < minimum || value > maximum) return undefined
    return value
  }

  function parseState(text) {
    if (typeof text !== "string" || text.length > 65536) return
    var value
    try { value = JSON.parse(text) } catch (e) { return }
    if (!value || typeof value !== "object" || Array.isArray(value)) return
    var required = [
      "schema", "present", "vendor", "card", "util", "memUtil", "sclkMhz", "mclkMhz",
      "tempEdge", "tempJunction", "tempMem", "tempGpu", "powerW", "fanRpm",
      "vramUsedG", "vramTotalG", "tempSensors", "alertTemp", "alertLabel"
    ]
    for (var requiredIndex = 0; requiredIndex < required.length; requiredIndex++) {
      if (!Object.prototype.hasOwnProperty.call(value, required[requiredIndex])) return
    }
    if (value.schema !== 1 || typeof value.present !== "boolean") return
    var clean = {
      schema: 1,
      present: value.present,
      vendor: null,
      card: null,
      util: null,
      memUtil: null,
      sclkMhz: null,
      mclkMhz: null,
      tempEdge: null,
      tempJunction: null,
      tempMem: null,
      tempGpu: null,
      powerW: null,
      fanRpm: null,
      vramUsedG: null,
      vramTotalG: null,
      tempSensors: [],
      alertTemp: null,
      alertLabel: ""
    }
    if (!value.present) {
      var absentFields = [
        "vendor", "card", "util", "memUtil", "sclkMhz", "mclkMhz", "tempEdge",
        "tempJunction", "tempMem", "tempGpu", "powerW", "fanRpm", "vramUsedG",
        "vramTotalG", "alertTemp"
      ]
      for (var absentIndex = 0; absentIndex < absentFields.length; absentIndex++) {
        if (value[absentFields[absentIndex]] !== null) return
      }
      if (!Array.isArray(value.tempSensors) || value.tempSensors.length !== 0 || value.alertLabel !== "") return
      root.state = clean
      return
    }
    if (value.vendor !== "AMD" && value.vendor !== "NVIDIA") return
    if (typeof value.card !== "string" || value.card.length === 0 || value.card.length > 128 || /[\x00-\x1f]/.test(value.card)) return
    clean.vendor = value.vendor
    clean.card = value.card
    var fields = [
      ["util", 0, 100], ["memUtil", 0, 100],
      ["sclkMhz", 0, 1000000], ["mclkMhz", 0, 1000000],
      ["tempEdge", -100, 200], ["tempJunction", -100, 200],
      ["tempMem", -100, 200], ["tempGpu", -100, 200],
      ["powerW", 0, 1000000], ["fanRpm", 0, 1000000],
      ["vramUsedG", 0, 1000000], ["vramTotalG", 0, 1000000]
    ]
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i][0]
      var parsed = root.numberOrNull(value[field], fields[i][1], fields[i][2])
      if (parsed === undefined) return
      clean[field] = parsed
    }
    if (clean.vendor === "NVIDIA" && clean.tempJunction !== null) return
    if (value.tempSensors !== undefined && !Array.isArray(value.tempSensors)) return
    if (Array.isArray(value.tempSensors)) {
      if (value.tempSensors.length > 32) return
      var seenSensors = {}
      for (var s = 0; s < value.tempSensors.length; s++) {
        var sensor = value.tempSensors[s]
        if (!sensor || typeof sensor !== "object" || Array.isArray(sensor)) return
        if (typeof sensor.label !== "string" || sensor.label.length === 0 || sensor.label.length > 64 || /[\x00-\x1f]/.test(sensor.label)) return
        if (["edge", "junction", "memory", "core", "gpu", "unknown"].indexOf(sensor.kind) < 0) return
        if (clean.vendor === "NVIDIA" && sensor.kind === "junction") return
        var sensorValue = root.numberOrNull(sensor.value, -100, 200)
        if (sensorValue === undefined || sensorValue === null) return
        var sensorIdentity = sensor.label + "\u0000" + sensor.kind
        if (seenSensors[sensorIdentity] === true) return
        seenSensors[sensorIdentity] = true
        clean.tempSensors.push({ label: sensor.label, value: sensorValue, kind: sensor.kind })
      }
    }
    if (clean.vramUsedG !== null && clean.vramTotalG !== null && clean.vramUsedG > clean.vramTotalG) return
    var alertTemp = root.numberOrNull(value.alertTemp, -100, 200)
    if (alertTemp === undefined || typeof value.alertLabel !== "string" || value.alertLabel.length > 32 || /[\x00-\x1f]/.test(value.alertLabel)) return
    var expectedAlertTemp = null
    var expectedAlertLabel = ""
    if (clean.vendor === "AMD" && clean.tempJunction !== null) {
      expectedAlertTemp = clean.tempJunction
      expectedAlertLabel = "Junction"
    } else if (clean.vendor === "NVIDIA" && clean.tempGpu !== null) {
      expectedAlertTemp = clean.tempGpu
      expectedAlertLabel = "GPU core"
    }
    if (alertTemp !== expectedAlertTemp || value.alertLabel !== expectedAlertLabel) return
    clean.alertTemp = alertTemp
    clean.alertLabel = value.alertLabel
    root.state = clean
  }

  function refresh() {
    if (root.statePath !== "") stateFile.reload()
  }

  function setDetailOpen(value) {
    root.detailOpen = value === true
    if (root.detailOpen && root.state.vendor === "NVIDIA" && !processProc.running)
      processProc.running = true
  }

  function parseProcesses(text) {
    var lines = String(text || "").split("\n")
    var rows = []
    for (var i = 0; i < lines.length && rows.length < 8; i++) {
      var line = lines[i].trim()
      if (!line) continue
      var parts = line.split(",")
      if (parts.length < 3) continue
      var pid = Number(parts[0])
      var memory = Number(parts[parts.length - 1])
      if (!isFinite(pid) || !isFinite(memory)) continue
      var name = parts.slice(1, parts.length - 1).join(",").trim()
      if (!name) continue
      rows.push({ pid: pid, name: name.slice(0, 64), memory: memory })
    }
    root.gpuProcesses = rows
  }

  Process {
    id: processProc
    command: ["/usr/bin/nvidia-smi",
      "--query-compute-apps=pid,process_name,used_memory",
      "--format=csv,noheader,nounits"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseProcesses(text)
    }
    stderr: StdioCollector { waitForEnd: true }
  }

  Timer {
    interval: 3000
    repeat: true
    running: root.detailOpen && root.state.vendor === "NVIDIA"
    onTriggered: if (!processProc.running) processProc.running = true
  }

  Component.onCompleted: root.refresh()
}
