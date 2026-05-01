// Copyright (c) 2018- PPSSPP Project.

// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, version 2.0 or later versions.

// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License 2.0 for more details.

// A copy of the GPL 2.0 should have been included with the program.
// If not, see http://www.gnu.org/licenses/

// Official git repository and contact information can be found at
// https://github.com/hrydgard/ppsspp and http://www.ppsspp.org/.

#include "Common/Data/Hash/Hash.h"
#include "Common/StringUtils.h"
#include "Common/System/System.h"
#include "Core/Config.h"
#include "Core/ConfigValues.h"
#include "Core/Debugger/WebSocket/GameBroadcaster.h"
#include "Core/Debugger/WebSocket/GameSubscriber.h"
#include "Core/Debugger/WebSocket/WebSocketUtils.h"
#include "Core/ELF/ParamSFO.h"
#include "Core/HLE/sceAudio.h"
#include "Core/System.h"
#include "GPU/GPU.h"

static uint32_t g_audioTraceCallIndex = 0;
static uint32_t g_audioTraceEventIndex = 0;

DebuggerSubscriber *WebSocketGameInit(DebuggerEventHandlerMap &map) {
	map["audio.trace.get"] = &WebSocketAudioTraceGet;
	map["debugger.capabilities"] = &WebSocketDebuggerCapabilities;
	map["game.reset"] = &WebSocketGameReset;
	map["game.status"] = &WebSocketGameStatus;
	map["settings.fingerprint"] = &WebSocketSettingsFingerprint;
	map["version"] = &WebSocketVersion;

	return nullptr;
}

static const char *CPUCoreToDebuggerString(int cpuCore) {
	switch ((CPUCore)cpuCore) {
	case CPUCore::INTERPRETER:
		return "interpreter";
	case CPUCore::JIT:
		return "jit";
	case CPUCore::IR_INTERPRETER:
		return "ir_interpreter";
	case CPUCore::JIT_IR:
		return "jit_ir";
	default:
		return "unknown";
	}
}

static std::string DeterminismSettingsPayload() {
	return StringFromFormat(
		"schema=1\n"
		"cpu.core=%s\n"
		"cpu.fast_memory=%d\n"
		"cpu.jit_disable_flags=%08x\n"
		"gpu.backend=%s\n"
		"gpu.software_rendering=%d\n"
		"gpu.hardware_transform=%d\n"
		"gpu.software_skinning=%d\n"
		"gpu.skip_buffer_effects=%d\n"
		"gpu.disable_range_culling=%d\n"
		"gpu.depth_raster_mode=%d\n"
		"gpu.internal_resolution=%d\n"
		"gpu.texture_filtering=%d\n"
		"gpu.texture_scaling_level=%d\n"
		"gpu.texture_scaling_type=%d\n"
		"gpu.texture_deposterize=%d\n"
		"gpu.bloom_hack=%d\n"
		"clock.locked_cpu_speed=%d\n"
		"clock.force_lag_sync=%d\n"
		"audio.enable_sound=%d\n"
		"audio.playback_mode=%d\n"
		"audio.buffer_size=%d\n"
		"hacks.func_replacements=%d\n"
		"hacks.ignore_bad_mem_access=%d\n"
		"hacks.i_o_timing_method=%d\n",
		CPUCoreToDebuggerString(g_Config.iCpuCore),
		(int)g_Config.bFastMemory,
		g_Config.uJitDisableFlags,
		GPUBackendToString((GPUBackend)g_Config.iGPUBackend).c_str(),
		(int)g_Config.bSoftwareRendering,
		(int)g_Config.bHardwareTransform,
		(int)g_Config.bSoftwareSkinning,
		(int)g_Config.bSkipBufferEffects,
		(int)g_Config.bDisableRangeCulling,
		g_Config.iDepthRasterMode,
		g_Config.iInternalResolution,
		g_Config.iTexFiltering,
		g_Config.iTexScalingLevel,
		g_Config.iTexScalingType,
		(int)g_Config.bTexDeposterize,
		g_Config.iBloomHack,
		g_Config.iLockedCPUSpeed,
		(int)g_Config.bForceLagSync,
		(int)g_Config.bEnableSound,
		g_Config.iAudioPlaybackMode,
		g_Config.iAudioBufferSize,
		(int)g_Config.bFuncReplacements,
		(int)g_Config.bIgnoreBadMemAccess,
		g_Config.iIOTimingMethod);
}

// Reset emulation (game.reset)
//
// Use this if you need to break on start and do something before the game starts.
//
// Parameters:
//  - break: optional boolean, true to break CPU on start.  Use cpu.resume afterward.
//
// Response (same event name) with no extra data or error.
void WebSocketGameReset(DebuggerRequest &req) {
	if (PSP_GetBootState() != BootState::Complete)
		return req.Fail("Game not running");

	bool needBreak = false;
	if (!req.ParamBool("break", &needBreak, DebuggerParamType::OPTIONAL))
		return;

	if (needBreak)
		PSP_CoreParameter().startBreak = true;

	// We can only support async resets here. A lot of the stuff in init must happen on the EmuThread,
	// and we are not on it here.
	System_PostUIMessage(UIMessage::REQUEST_GAME_RESET);

	req.Respond();
}

// Check game status (game.status)
//
// No parameters.
//
// Response (same event name):
//  - game: null or an object with properties:
//     - id: string disc ID (such as ULUS12345.)
//     - version: string disc version.
//     - title: string game title.
//  - paused: boolean, true when gameplay is paused (not the same as stepping.)
//  - eventIndex: number of the latest emitted game.* broadcast event (0 if none yet.)
void WebSocketGameStatus(DebuggerRequest &req) {
	JsonWriter &json = req.Respond();
	if (PSP_GetBootState() == BootState::Complete) {
		json.pushDict("game");
		json.writeString("id", g_paramSFO.GetDiscID());
		json.writeString("version", g_paramSFO.GetValueString("DISC_VERSION"));
		json.writeString("title", g_paramSFO.GetValueString("TITLE"));
		json.pop();
	} else {
		json.writeNull("game");
	}
	json.writeBool("paused", GetUIState() == UISTATE_PAUSEMENU);
	json.writeInt("eventIndex", (int)WebSocketGameEventIndex());
}

// List debugger protocol capabilities (debugger.capabilities)
//
// No parameters.
//
// Response (same event name):
//  - schemaVersion: integer version for this capabilities payload.
//  - hooks: object mapping optional hook families to support booleans.
//  - events: array of event names provided by this build for hook-tracker clients.
void WebSocketDebuggerCapabilities(DebuggerRequest &req) {
	JsonWriter &json = req.Respond();
	json.writeInt("schemaVersion", 1);

	json.pushDict("hooks");
	json.writeBool("settingsFingerprint", true);
	json.writeBool("cpuFrameEventIndex", true);
	json.writeBool("traceLimits", true);
	json.writeBool("geTrace", true);
	json.writeBool("audioTrace", true);
	json.writeBool("memoryTrace", true);
	json.writeBool("traceSnapshotOnly", true);
	json.pop();

	json.pushArray("events");
	json.writeString("debugger.capabilities");
	json.writeString("settings.fingerprint");
	json.writeString("cpu.status");
	json.writeString("cpu.stepping");
	json.writeString("gpu.trace.get");
	json.writeString("audio.trace.get");
	json.writeString("memory.trace.get");
	json.pop();
}

// Report determinism-relevant settings fingerprint (settings.fingerprint)
//
// No parameters.
//
// Response (same event name):
//  - schemaVersion: integer version for the settings list.
//  - algorithm: string hash algorithm used.
//  - fingerprint: stable hash of CPU/GPU/backend/clock/hack settings.
//  - settings: object containing the normalized setting values included in the hash.
void WebSocketSettingsFingerprint(DebuggerRequest &req) {
	const std::string payload = DeterminismSettingsPayload();

	JsonWriter &json = req.Respond();
	json.writeInt("schemaVersion", 1);
	json.writeString("algorithm", "adler32");
	json.writeString("fingerprint", StringFromFormat("%08x", hash::Adler32(payload)));

	json.pushDict("settings");
	json.writeString("cpuCore", CPUCoreToDebuggerString(g_Config.iCpuCore));
	json.writeBool("fastMemory", g_Config.bFastMemory);
	json.writeUint("jitDisableFlags", g_Config.uJitDisableFlags);
	json.writeString("gpuBackend", GPUBackendToString((GPUBackend)g_Config.iGPUBackend));
	json.writeBool("softwareRendering", g_Config.bSoftwareRendering);
	json.writeBool("hardwareTransform", g_Config.bHardwareTransform);
	json.writeBool("softwareSkinning", g_Config.bSoftwareSkinning);
	json.writeBool("skipBufferEffects", g_Config.bSkipBufferEffects);
	json.writeBool("disableRangeCulling", g_Config.bDisableRangeCulling);
	json.writeInt("depthRasterMode", g_Config.iDepthRasterMode);
	json.writeInt("internalResolution", g_Config.iInternalResolution);
	json.writeInt("textureFiltering", g_Config.iTexFiltering);
	json.writeInt("textureScalingLevel", g_Config.iTexScalingLevel);
	json.writeInt("textureScalingType", g_Config.iTexScalingType);
	json.writeBool("textureDeposterize", g_Config.bTexDeposterize);
	json.writeInt("bloomHack", g_Config.iBloomHack);
	json.writeInt("lockedCPUSpeed", g_Config.iLockedCPUSpeed);
	json.writeBool("forceLagSync", g_Config.bForceLagSync);
	json.writeBool("enableSound", g_Config.bEnableSound);
	json.writeInt("audioPlaybackMode", g_Config.iAudioPlaybackMode);
	json.writeInt("audioBufferSize", g_Config.iAudioBufferSize);
	json.writeBool("funcReplacements", g_Config.bFuncReplacements);
	json.writeBool("ignoreBadMemAccess", g_Config.bIgnoreBadMemAccess);
	json.writeInt("ioTimingMethod", g_Config.iIOTimingMethod);
	json.pop();
}

// Fetch deterministic audio trace events (audio.trace.get)
//
// Parameters:
//  - max_frames: optional bounded frame limit.
//  - max_events: optional bounded event limit.
//
// Response (same event name):
//  - supported: true (snapshot mode only in this build.)
//  - max_frames/max_events/truncated: normalized trace limit metadata.
//  - callIndex: monotonically increasing request index.
//  - events: bounded array of per-channel snapshot events.
void WebSocketAudioTraceGet(DebuggerRequest &req) {
	DebuggerTraceLimits limits;
	if (!DebuggerParseTraceLimits(req, &limits))
		return;

	JsonWriter &json = req.Respond();
	json.writeBool("supported", true);
	json.writeString("mode", "snapshot");
	json.writeString("capability", "audioTrace");
	DebuggerWriteTraceLimits(json, limits);
	json.writeInt("frame_span_start", gpuStats.numFlips);
	json.writeInt("frame_span_end", gpuStats.numFlips);
	json.writeInt("callIndex", (int)++g_audioTraceCallIndex);
	json.writeString("notes", "Current audio channel state snapshot only; per-sample audio event stream is not yet exposed.");

	json.pushArray("volatileFields");
	json.writeString("events[].sampleAddress");
	json.pop();

	const int callIndex = (int)g_audioTraceCallIndex;
	const int frameIndex = gpuStats.numFlips;
	int emitted = 0;
	int blockIndex = 0;
	bool truncated = limits.truncated;
	json.pushArray("events");
	for (size_t i = 0; i < PSP_AUDIO_CHANNEL_MAX + 1; ++i) {
		if ((uint32_t)emitted >= limits.maxEvents) {
			truncated = true;
			break;
		}
		const auto &ch = g_audioChans[i];
		json.pushDict();
		json.writeInt("frame_index", frameIndex);
		json.writeInt("callIndex", callIndex);
		json.writeInt("blockIndex", blockIndex++);
		json.writeInt("eventIndex", (int)++g_audioTraceEventIndex);
		json.writeString("type", "channelState");
		json.writeInt("channel", (int)i);
		json.writeBool("reserved", ch.reserved);
		json.writeUint("sampleAddress", ch.sampleAddress);
		json.writeUint("sampleCount", ch.sampleCount);
		json.writeUint("leftVolume", ch.leftVolume);
		json.writeUint("rightVolume", ch.rightVolume);
		json.writeUint("format", ch.format);
		json.writeBool("muted", ch.mute);
		json.writeInt("waitingThreads", (int)ch.waitingThreads.size());
		json.pop();
		++emitted;
	}
	json.pop();
	json.writeInt("eventCount", emitted);
	json.writeBool("eventsTruncated", truncated);
}

// Notify debugger version info (version)
//
// Parameters:
//  - name: string indicating name of app or tool.
//  - version: string version.
//
// Response (same event name):
//  - name: string, "PPSSPP" unless some special build.
//  - version: string, typically starts with "v" and may have git build info.
void WebSocketVersion(DebuggerRequest &req) {
	JsonWriter &json = req.Respond();

	std::string version = req.client->version;
	if (!req.ParamString("version", &version, DebuggerParamType::OPTIONAL_LOOSE))
		return;
	std::string name = req.client->name;
	if (!req.ParamString("name", &name, DebuggerParamType::OPTIONAL_LOOSE))
		return;

	req.client->version = version;
	req.client->name = name;

	json.writeString("name", "PPSSPP");
	json.writeString("version", PPSSPP_GIT_VERSION);
}
