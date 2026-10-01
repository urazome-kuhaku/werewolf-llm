/**
 * Read-only Pi tools for the seat-scoped Werewolf knowledge gateway.
 *
 * The extension intentionally has no filesystem, shell, or game-state access.
 * Its only authority is the bearer token supplied by the parent runtime and
 * the loopback URL supplied in the process environment.
 */

import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BASE_URL_ENV = "WEREWOLF_KNOWLEDGE_BASE_URL";
const TOKEN_ENV = "WEREWOLF_KNOWLEDGE_TOKEN";
const MAX_RESPONSE_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_TOKEN_LENGTH = 512;

type KnowledgeError = {
	code: string;
	message: string;
	status?: number;
	details?: unknown;
};

type ToolDetails = {
	ok: boolean;
	status: number;
	data?: unknown;
	error?: KnowledgeError;
};

type BoardParams = { id?: string };
type RoleParams = { role_id: string };
type MechanicParams = { mechanic_id: string };
type InteractionParams = { subjects: string[]; situation?: string };
type TopicParams = { topic_id: string };
type SearchParams = {
	query: string;
	kinds?: ("board" | "role" | "mechanic" | "interaction" | "topic")[];
	limit?: number;
};
type SkillStatusParams = Record<string, never>;

const kindSchema = Type.Union([
	Type.Literal("board"),
	Type.Literal("role"),
	Type.Literal("mechanic"),
	Type.Literal("interaction"),
	Type.Literal("topic"),
]);

function errorResult(status: number, error: KnowledgeError): {
	content: [{ type: "text"; text: string }];
	details: ToolDetails;
} {
	const details: ToolDetails = { ok: false, status, error };
	return {
		content: [{ type: "text", text: JSON.stringify({ status: "error", error }) }],
		details,
	};
}

function okResult(status: number, data: unknown): {
	content: [{ type: "text"; text: string }];
	details: ToolDetails;
} {
	return {
		content: [{ type: "text", text: JSON.stringify(data) }],
		details: { ok: true, status, data },
	};
}

function configuration(): { baseUrl: string; token: string } | KnowledgeError {
	const rawBaseUrl = process.env[BASE_URL_ENV]?.trim();
	const token = process.env[TOKEN_ENV];
	if (!rawBaseUrl) {
		return {
			code: "CONFIGURATION_ERROR",
			message: `${BASE_URL_ENV} is required`,
		};
	}
	if (!token || token.length > MAX_TOKEN_LENGTH || /[\r\n]/.test(token)) {
		return {
			code: "CONFIGURATION_ERROR",
			message: `${TOKEN_ENV} is missing or invalid`,
		};
	}

	let parsed: URL;
	try {
		parsed = new URL(rawBaseUrl);
	} catch {
		return {
			code: "CONFIGURATION_ERROR",
			message: `${BASE_URL_ENV} must be a valid loopback HTTP URL`,
		};
	}
	const normalizedPath = parsed.pathname.replace(/\/+$/, "");
	if (
		parsed.protocol !== "http:" ||
		parsed.hostname !== "127.0.0.1" ||
		parsed.username ||
		parsed.password ||
		parsed.search ||
		parsed.hash ||
		normalizedPath !== "/v1"
	) {
		return {
			code: "CONFIGURATION_ERROR",
			message: `${BASE_URL_ENV} must be an HTTP URL rooted at http://127.0.0.1/.../v1`,
		};
	}

	return { baseUrl: rawBaseUrl.replace(/\/+$/, ""), token };
}

function joinRoute(baseUrl: string, route: string): string {
	// Routes are constants owned by this extension. User supplied identifiers
	// are encoded as one path segment and can never replace the host or route.
	return `${baseUrl}${route}`;
}

function encodeId(value: string): string {
	return encodeURIComponent(value);
}

function resolveBoardId(id: string | undefined): string {
	return id === undefined || id.trim() === "" ? "current" : id;
}

async function readResponse(response: Response): Promise<string> {
	if (!response.body) {
		return "";
	}
	const reader = response.body.getReader();
	const chunks: Uint8Array[] = [];
	let size = 0;
	try {
		while (true) {
			const next = await reader.read();
			if (next.done) break;
			const chunk = next.value;
			size += chunk.byteLength;
			if (size > MAX_RESPONSE_BYTES) {
				await reader.cancel();
				throw new ResponseLimitError();
			}
			chunks.push(chunk);
		}
	} finally {
		reader.releaseLock();
	}
	const joined = new Uint8Array(size);
	let offset = 0;
	for (const chunk of chunks) {
		joined.set(chunk, offset);
		offset += chunk.byteLength;
	}
	return new TextDecoder().decode(joined);
}

class ResponseLimitError extends Error {
	constructor() {
		super("knowledge gateway response is too large");
		this.name = "ResponseLimitError";
	}
}

async function request(
	route: string,
	method: "GET" | "POST",
	body: unknown,
	signal: AbortSignal | undefined,
): Promise<{ status: number; data: unknown } | KnowledgeError> {
	const config = configuration();
	if ("code" in config) return config;

	const controller = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const abortFromCaller = () => controller.abort();
	if (signal) {
		if (signal.aborted) controller.abort();
		else signal.addEventListener("abort", abortFromCaller, { once: true });
	}
	if (!controller.signal.aborted) {
		timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
	}

	try {
		const response = await fetch(joinRoute(config.baseUrl, route), {
			method,
			headers: {
				Accept: "application/json",
				Authorization: `Bearer ${config.token}`,
				...(method === "POST" ? { "Content-Type": "application/json" } : {}),
			},
			body: method === "POST" ? JSON.stringify(body) : undefined,
			signal: controller.signal,
		});
		const text = await readResponse(response);
		let data: unknown;
		try {
			data = text ? JSON.parse(text) : {};
		} catch {
			return {
				code: "INVALID_RESPONSE",
				message: "knowledge gateway returned invalid JSON",
				status: response.status,
			};
		}
		if (!response.ok) {
			const error = extractGatewayError(data, response.status);
			return error;
		}
		if (!isRecord(data) || data.status !== "ok") {
			return {
				code: "INVALID_RESPONSE",
				message: "knowledge gateway returned an invalid success envelope",
				status: response.status,
			};
		}
		return { status: response.status, data };
	} catch (error) {
		if (error instanceof ResponseLimitError) {
			return { code: "RESPONSE_TOO_LARGE", message: error.message, status: 502 };
		}
		if (controller.signal.aborted) {
			const code = signal?.aborted ? "CANCELLED" : "TIMEOUT";
			return {
				code,
				message: code === "TIMEOUT" ? "knowledge gateway request timed out" : "knowledge query cancelled",
				status: 504,
			};
		}
		return { code: "GATEWAY_UNAVAILABLE", message: "knowledge gateway request failed", status: 502 };
	} finally {
		if (timer) clearTimeout(timer);
		if (signal) signal.removeEventListener("abort", abortFromCaller);
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractGatewayError(data: unknown, status: number): KnowledgeError {
	if (isRecord(data) && isRecord(data.error)) {
		const code = typeof data.error.code === "string" ? data.error.code : "GATEWAY_ERROR";
		const message = typeof data.error.message === "string" ? data.error.message : "knowledge query failed";
		return { code, message, status, details: data.error.details };
	}
	return { code: "GATEWAY_ERROR", message: "knowledge gateway request failed", status };
}

async function query(
	route: string,
	method: "GET" | "POST",
	body: unknown,
	signal: AbortSignal | undefined,
) {
	const response = await request(route, method, body, signal);
	if ("code" in response) return errorResult(response.status ?? 502, response);
	return okResult(response.status, response.data);
}

const getBoard = defineTool({
	name: "get_board",
	label: "Knowledge: board",
	description: "Read the current board overview, composition, win conditions, flow, and reading plan.",
	promptSnippet: "Read the current board rules and reading plan.",
	promptGuidelines: ["Read this before acting and reread it when the board flow is unclear."],
	parameters: Type.Object({
		id: Type.Optional(Type.String({ description: "Board ID; omit or leave blank to read the current board." })),
	}),
	async execute(_toolCallId, params: BoardParams, signal) {
		const id = resolveBoardId(params.id);
		return query(`/board/${encodeId(id)}`, "GET", undefined, signal);
	},
});

const getRole = defineTool({
	name: "get_role",
	label: "Knowledge: role",
	description: "Read the effective rules for a role in the current board snapshot.",
	promptSnippet: "Read a role's effective rules for this board.",
	parameters: Type.Object({
		role_id: Type.String({ description: "Role ID or unambiguous role alias." }),
	}),
	async execute(_toolCallId, params: RoleParams, signal) {
		return query(`/role/${encodeId(params.role_id)}`, "GET", undefined, signal);
	},
});

const getMechanic = defineTool({
	name: "get_mechanic",
	label: "Knowledge: mechanic",
	description: "Read one published game mechanic from the current board snapshot.",
	promptSnippet: "Read a specific game mechanic.",
	parameters: Type.Object({
		mechanic_id: Type.String({ description: "Mechanic ID, topic, or unambiguous alias." }),
	}),
	async execute(_toolCallId, params: MechanicParams, signal) {
		return query(`/mechanic/${encodeId(params.mechanic_id)}`, "GET", undefined, signal);
	},
});

const getInteraction = defineTool({
	name: "get_interaction",
	label: "Knowledge: interaction",
	description:
		"Read an exact interaction from the current board. If the query returns NOT_FOUND, use search_rules with kinds=['interaction'] first to discover the exact subjects and situation_key, then retry.",
	promptSnippet: "Search interaction rules for exact keys before resolving a cross role situation.",
	promptGuidelines: [
		"subjects and situation are exact logical keys from the published snapshot; do not substitute broad role names.",
		"If the exact query returns NOT_FOUND, follow its search_rules guidance and retry with the discovered keys.",
	],
	parameters: Type.Object({
		subjects: Type.Array(
			Type.String({ description: "At least two exact role, ability, or mechanic logical IDs." }),
			{ minItems: 2 },
		),
		situation: Type.Optional(
			Type.String({ description: "Exact situation_key discovered from interaction search results." }),
		),
	}),
	async execute(_toolCallId, params: InteractionParams, signal) {
		return query("/interactions/query", "POST", params, signal);
	},
});

const getRuleTopic = defineTool({
	name: "get_rule_topic",
	label: "Knowledge: topic",
	description: "Read one exact rule topic or board chapter from the current snapshot.",
	promptSnippet: "Read an exact board rule topic.",
	parameters: Type.Object({
		topic_id: Type.String({ description: "Topic ID such as board.flow or board.death." }),
	}),
	async execute(_toolCallId, params: TopicParams, signal) {
		return query(`/topic/${encodeId(params.topic_id)}`, "GET", undefined, signal);
	},
});

const searchRules = defineTool({
	name: "search_rules",
	label: "Knowledge: search",
	description: "Search only the current published board snapshot when the exact rule reference is unknown.",
	promptSnippet: "Search the current board rules for a term or question.",
	promptGuidelines: ["Use this when a rule is missing from context instead of relying on training memory."],
	parameters: Type.Object({
		query: Type.String({ minLength: 1, description: "A concise rule question or search phrase." }),
		kinds: Type.Optional(Type.Array(kindSchema, { minItems: 1 })),
		limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 8, default: 8 })),
	}),
	async execute(_toolCallId, params: SearchParams, signal) {
		return query("/search", "POST", params, signal);
	},
});

const getSkillStatus = defineTool({
	name: "get_skill_status",
	label: "Game: skill status",
	description:
		"Read the authenticated player's current role abilities, counters, resources, trigger conditions, and legal action windows.",
	promptSnippet: "Check your current skill counters and legal action windows before acting.",
	promptGuidelines: [
		"This is a read-only seat-scoped status check; it does not submit or consume an action.",
	],
	parameters: Type.Object({}),
	async execute(_toolCallId, _params: SkillStatusParams, signal) {
		return query("/game/skills/me", "GET", undefined, signal);
	},
});

export default function registerWerewolfKnowledge(pi: ExtensionAPI): void {
	pi.registerTool(getBoard);
	pi.registerTool(getRole);
	pi.registerTool(getMechanic);
	pi.registerTool(getInteraction);
	pi.registerTool(getRuleTopic);
	pi.registerTool(searchRules);
	pi.registerTool(getSkillStatus);
}
