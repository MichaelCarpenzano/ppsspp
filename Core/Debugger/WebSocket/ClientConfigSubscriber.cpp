// Copyright (c) 2023- PPSSPP Project.

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

#include "Core/Debugger/WebSocket/ClientConfigSubscriber.h"
#include <cstdint>
#include "Core/Debugger/WebSocket/WebSocketUtils.h"
#include "Common/StringUtils.h"
#include "Core/Debugger/WebSocket/GameBroadcaster.h"
#include "Core/Debugger/WebSocket/SteppingBroadcaster.h"

static uint64_t FNV1A64Append(uint64_t hash, const char *data) {
	for (const unsigned char *p = (const unsigned char *)data; *p; ++p) {
		hash ^= *p;
		hash *= 1099511628211ULL;
	}
	return hash;
}

static std::string ClientSettingsFingerprint(const WebSocketClientInfo &client) {
	uint64_t hash = 1469598103934665603ULL;
	hash = FNV1A64Append(hash, client.name.c_str());
	hash = FNV1A64Append(hash, "\n");
	hash = FNV1A64Append(hash, client.version.c_str());
	hash = FNV1A64Append(hash, "\n");
	for (const auto &[key, value] : client.disallowed) {
		hash = FNV1A64Append(hash, key.c_str());
		hash = FNV1A64Append(hash, "=");
		hash = FNV1A64Append(hash, value ? "1" : "0");
		hash = FNV1A64Append(hash, ";");
	}
	return StringFromFormat("%016llx", (unsigned long long)hash);
}

DebuggerSubscriber *WebSocketClientConfigInit(DebuggerEventHandlerMap & map) {
	map["broadcast.config.get"] = &WebSocketBroadcastConfigGet;
	map["broadcast.config.set"] = &WebSocketBroadcastConfigSet;
	map["broadcast.capabilities.get"] = &WebSocketBroadcastCapabilitiesGet;
	map["broadcast.settingsFingerprint.get"] = &WebSocketBroadcastSettingsFingerprintGet;

	return nullptr;
}


// Request the current client broadcast configuration (broadcast.config.get)
//
// No parameters.
//
// Response (same event name):
//  - disallowed: object with optional boolean fields:
//     - logger: whether logger events are disallowed
//     - game: whether game events are disallowed
//     - stepping: whether stepping events are disallowed
//     - input: whether input events are disallowed
void WebSocketBroadcastConfigGet(DebuggerRequest & req) {
	JsonWriter &json = req.Respond();
	const auto& disallowed_config = req.client->disallowed;

	json.pushDict("disallowed");

	for (const auto &[name, status] : disallowed_config) {
		if (status)
			json.writeBool(name, true);
	}

	json.end();
}

// Query WebSocket debugger contract and metadata support (broadcast.capabilities.get)
//
// No parameters.
//
// Response (same event name):
//  - contractVersion: number, current metadata contract version for this event group.
//  - requestEvents: array of strings, request event names in this group.
//  - metadata: object describing optional metadata fields available on selected events.
//  - settingsFingerprint: object with algorithm details.
void WebSocketBroadcastCapabilitiesGet(DebuggerRequest &req) {
	JsonWriter &json = req.Respond();
	json.writeInt("contractVersion", 1);

	json.pushArray("requestEvents");
	json.writeString("broadcast.config.get");
	json.writeString("broadcast.config.set");
	json.writeString("broadcast.capabilities.get");
	json.writeString("broadcast.settingsFingerprint.get");
	json.pop();

	json.pushDict("metadata");
	json.pushDict("cpu.stepping");
	json.writeBool("eventIndex", true);
	json.writeBool("frameIndex", true);
	json.writeBool("steppingCounter", true);
	json.pop();
	json.pushDict("cpu.resume");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pushDict("cpu.status");
	json.writeBool("eventIndex", true);
	json.writeBool("frameIndex", true);
	json.writeBool("steppingCounter", true);
	json.pop();
	json.pushDict("game.status");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pushDict("game.start");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pushDict("game.pause");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pushDict("game.resume");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pushDict("game.quit");
	json.writeBool("eventIndex", true);
	json.pop();
	json.pop();

	json.pushDict("settingsFingerprint");
	json.writeString("algorithm", "fnv1a64");
	json.writeBool("includesClientIdentity", true);
	json.writeBool("includesDisallowedConfig", true);
	json.pop();
}

// Compute deterministic client settings fingerprint (broadcast.settingsFingerprint.get)
//
// No parameters.
//
// Response (same event name):
//  - fingerprint: lowercase hex string of current config identity.
//  - algorithm: string hash name.
//  - client: object with optional "name" and "version".
//  - disallowed: object containing all known disallowed broadcaster keys.
void WebSocketBroadcastSettingsFingerprintGet(DebuggerRequest &req) {
	JsonWriter &json = req.Respond();
	json.writeString("fingerprint", ClientSettingsFingerprint(*req.client));
	json.writeString("algorithm", "fnv1a64");

	json.pushDict("client");
	if (!req.client->name.empty())
		json.writeString("name", req.client->name);
	if (!req.client->version.empty())
		json.writeString("version", req.client->version);
	json.pop();

	json.pushDict("disallowed");
	for (const auto &[name, status] : req.client->disallowed)
		json.writeBool(name, status);
	json.pop();

	json.pushDict("indices");
	json.writeInt("stepping", (int)WebSocketSteppingEventIndex());
	json.writeInt("game", (int)WebSocketGameEventIndex());
	json.pop();
}

// Update the current client broadcast configuration (broadcast.config.set)
//
// Parameters:
//  - disallowed: object with boolean fields (all of them are optional):
//     - logger: new logger config state
//     - game: new game config state
//     - stepping: new stepping config state
//     - input: new input config state
//
// Response (same event name):
//  - disallowed: object with optional boolean fields:
//     - logger: whether logger events are now disallowed
//     - game: whether game events are now disallowed
//     - stepping: whether stepping events are now disallowed
//     - input: whether input events are now disallowed
void WebSocketBroadcastConfigSet(DebuggerRequest & req) {
	JsonWriter &json = req.Respond();
	auto& disallowed_config = req.client->disallowed;

	const JsonNode *jsonDisallowed = req.data.get("disallowed");
	if (!jsonDisallowed) {
		return req.Fail("Missing 'disallowed' parameter");
	}
	if (jsonDisallowed->value.getTag() != JSON_OBJECT) {
		return req.Fail("Invalid 'disallowed' parameter type");
	}

	for (const JsonNode *broadcaster : jsonDisallowed->value) {
		auto it = disallowed_config.find(broadcaster->key);
		if (it == disallowed_config.end()) {
			return req.Fail(StringFromFormat("Unsupported 'disallowed' object key '%s'", broadcaster->key));
		}

		if (broadcaster->value.getTag() == JSON_TRUE) {
			it->second = true;
		}
		else if (broadcaster->value.getTag() == JSON_FALSE) {
			it->second = false;
		}
		else if (broadcaster->value.getTag() != JSON_NULL) {
			return req.Fail(StringFromFormat("Unsupported 'disallowed' object type for key '%s'", broadcaster->key));
		}
	}

	json.pushDict("disallowed");

	for (const auto &[name, status] : disallowed_config) {
		if (status)
			json.writeBool(name, true);
	}

	json.end();
}
