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

#include "Common/Data/Encoding/Base64.h"
#include "Common/File/FileUtil.h"
#include "Core/Debugger/WebSocket/GPURecordSubscriber.h"
#include "Core/Debugger/WebSocket/WebSocketUtils.h"
#include "Core/System.h"
#include "GPU/Debugger/Record.h"
#include "GPU/GPU.h"
#include "GPU/Common/GPUDebugInterface.h"

static uint32_t g_gpuTraceCallIndex = 0;
static uint32_t g_gpuTraceEventIndex = 0;

struct WebSocketGPURecordState : public DebuggerSubscriber {
	~WebSocketGPURecordState();
	void Dump(DebuggerRequest &req);
	void TraceGet(DebuggerRequest &req);

	void Broadcast(net::WebSocketServer *ws) override;

protected:
	bool pending_ = false;
	std::string lastTicket_;
	Path lastFilename_;
};

DebuggerSubscriber *WebSocketGPURecordInit(DebuggerEventHandlerMap &map) {
	auto p = new WebSocketGPURecordState();
	map["gpu.record.dump"] = [p](DebuggerRequest &req) { p->Dump(req); };
	map["gpu.trace.get"] = [p](DebuggerRequest &req) { p->TraceGet(req); };

	return p;
}

WebSocketGPURecordState::~WebSocketGPURecordState() {
	// Clear the callback to hopefully avoid a crash.
	if (pending_)
		gpuDebug->GetRecorder()->ClearCallback();
}

// Begin recording (gpu.record.dump)
//
// No parameters.
//
// Response (same event name):
//  - uri: data: URI containing debug dump data.
//
// Note: recording may take a moment.
void WebSocketGPURecordState::Dump(DebuggerRequest &req) {
	if (PSP_GetBootState() != BootState::Complete) {
		return req.Fail("CPU not started");
	}

	bool result = gpuDebug->GetRecorder()->RecordNextFrame([=](const Path &filename) {
		lastFilename_ = filename;
		pending_ = false;
	});

	if (!result) {
		return req.Fail("Recording already in progress");
	}

	pending_ = true;

	const JsonNode *value = req.data.get("ticket");
	lastTicket_ = value ? json_stringify(value) : "";
}

// Fetch deterministic GE trace events (gpu.trace.get)
//
// Parameters:
//  - max_frames: optional bounded frame limit.
//  - max_events: optional bounded event limit.
//
// Response (same event name):
//  - supported: true (snapshot mode only in this build.)
//  - capability: "geTrace".
//  - max_frames/max_events/truncated: normalized trace limit metadata.
void WebSocketGPURecordState::TraceGet(DebuggerRequest &req) {
	DebuggerTraceLimits limits;
	if (!DebuggerParseTraceLimits(req, &limits))
		return;

	JsonWriter &json = req.Respond();
	json.writeBool("supported", true);
	json.writeString("mode", "snapshot");
	json.writeString("capability", "geTrace");
	DebuggerWriteTraceLimits(json, limits);
	json.writeInt("frame_span_start", gpuStats.numFlips);
	json.writeInt("frame_span_end", gpuStats.numFlips);
	json.writeInt("callIndex", (int)++g_gpuTraceCallIndex);
	json.writeString("notes", "Current-frame GPU counters only; deep GE command stream events are not yet exposed.");

	// We provide one-frame deterministic snapshots from existing GPU counters.
	json.pushArray("volatileFields");
	json.writeString("events[].msProcessingDisplayLists");
	json.writeString("events[].msPrepareDepth");
	json.writeString("events[].msCullDepth");
	json.writeString("events[].msRasterizeDepth");
	json.writeString("events[].msRasterTimeAvailable");
	json.pop();

	int blockIndex = 0;
	int emitted = 0;
	bool truncated = limits.truncated || limits.maxFrames == 0;
	const int callIndex = (int)g_gpuTraceCallIndex;
	const int frameIndex = gpuStats.numFlips;
	json.pushArray("events");
	auto emitCounter = [&](const char *name, int value) {
		if ((uint32_t)emitted >= limits.maxEvents) {
			truncated = true;
			return;
		}
		json.pushDict();
		json.writeInt("frame_index", frameIndex);
		json.writeInt("callIndex", callIndex);
		json.writeInt("blockIndex", blockIndex++);
		json.writeInt("eventIndex", (int)++g_gpuTraceEventIndex);
		json.writeString("type", "counter");
		json.writeString("counter", name);
		json.writeInt("value", value);
		json.pop();
		++emitted;
	};
	auto emitFloatCounter = [&](const char *name, double value) {
		if ((uint32_t)emitted >= limits.maxEvents) {
			truncated = true;
			return;
		}
		json.pushDict();
		json.writeInt("frame_index", frameIndex);
		json.writeInt("callIndex", callIndex);
		json.writeInt("blockIndex", blockIndex++);
		json.writeInt("eventIndex", (int)++g_gpuTraceEventIndex);
		json.writeString("type", "counter");
		json.writeString("counter", name);
		json.writeFloat("value", value);
		json.pop();
		++emitted;
	};

	emitCounter("numFlips", gpuStats.numFlips);
	emitCounter("numDrawCalls", gpuStats.numDrawCalls);
	emitCounter("numVertsSubmitted", gpuStats.numVertsSubmitted);
	emitCounter("numVertsDecoded", gpuStats.numVertsDecoded);
	emitCounter("numFlushes", gpuStats.numFlushes);
	emitCounter("numClears", gpuStats.numClears);
	emitCounter("numBlockTransfers", gpuStats.numBlockTransfers);
	emitCounter("numReadbacks", gpuStats.numReadbacks);
	emitCounter("numUploads", gpuStats.numUploads);
	emitCounter("numTextureInvalidations", gpuStats.numTextureInvalidations);
	emitCounter("vertexGPUCycles", gpuStats.vertexGPUCycles);
	emitCounter("otherGPUCycles", gpuStats.otherGPUCycles);
	emitFloatCounter("msProcessingDisplayLists", gpuStats.msProcessingDisplayLists);
	emitFloatCounter("msPrepareDepth", gpuStats.msPrepareDepth);
	emitFloatCounter("msCullDepth", gpuStats.msCullDepth);
	emitFloatCounter("msRasterizeDepth", gpuStats.msRasterizeDepth);
	emitFloatCounter("msRasterTimeAvailable", gpuStats.msRasterTimeAvailable);
	json.pop();

	json.writeInt("eventCount", emitted);
	json.writeBool("eventsTruncated", truncated);
}

// This handles the asynchronous gpu.record.dump response.
void WebSocketGPURecordState::Broadcast(net::WebSocketServer *ws) {
	if (!lastFilename_.empty()) {
		FILE *fp = File::OpenCFile(lastFilename_, "rb");
		if (!fp) {
			lastFilename_.clear();
			return;
		}

		// We write directly to the stream since this is a large chunk of data.
		ws->AddFragment(false, R"({"event":"gpu.record.dump")");
		if (!lastTicket_.empty()) {
			ws->AddFragment(false, R"(,"ticket":)");
			ws->AddFragment(false, lastTicket_);
		}
		ws->AddFragment(false, R"(,"uri":"data:application/octet-stream;base64,)");

		// Divisible by 3 for base64 reasons.
		const size_t BUF_SIZE = 16383;
		std::vector<uint8_t> buf;
		buf.resize(BUF_SIZE);
		while (!feof(fp)) {
			size_t bytes = fread(&buf[0], 1, BUF_SIZE, fp);
			ws->AddFragment(false, Base64Encode(&buf[0], bytes));
		}
		fclose(fp);

		ws->AddFragment(true, R"("})");

		lastFilename_.clear();
		lastTicket_.clear();
	}
}
