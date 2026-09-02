{{- define "tofu.name" -}}
tofu
{{- end }}

{{- define "tofu.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "tofu.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tofu.labels" -}}
app.kubernetes.io/name: {{ include "tofu.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end }}

{{- define "tofu.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tofu.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
tofu.openai.com/process-role: {{ .role }}
{{- end }}

{{- define "tofu.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tofu.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "tofu.image" -}}
{{- $image := .root.Values.images.api -}}
{{- if eq .role "worker" -}}
{{- $image = .root.Values.images.worker -}}
{{- end -}}
{{- $digest := required (printf "images.%s.digest is required" .role) $image.digest -}}
{{- printf "%s@%s" $image.repository $digest -}}
{{- end }}

{{- define "tofu.apiImage" -}}
{{- $digest := required "images.api.digest is required" .Values.images.api.digest -}}
{{- printf "%s@%s" .Values.images.api.repository $digest -}}
{{- end }}

{{/*
The distributed topology, storage hand-off and secret-file locations are chart
authority.  Appending a duplicate EnvVar through extraEnv is accepted by the
Kubernetes API and the later value wins at container start, so schema
validation alone is not a sufficient release boundary.  Keep this template
guard as the fail-closed authority and let extraEnv carry only unrelated,
explicitly named application settings.
*/}}
{{- define "tofu.validateExtraEnv" -}}
{{- $protected := list
      "MALLOC_ARENA_MAX"
      "TOFU_AUTH_MODE"
      "TOFU_DB_BACKEND"
      "TOFU_DEPLOYMENT_MODE"
      "TOFU_DISTRIBUTED_PREVIEW_MODE"
      "TOFU_POSTGRES_DSN"
      "TOFU_POSTGRES_DSN_FILE"
      "TOFU_PROCESS_ROLE"
      "TOFU_REDIS_URL"
      "TOFU_REDIS_URL_FILE"
      "TOFU_REPLICA_ID"
      "TOFU_REPLICA_RING"
      "TOFU_REQUIRE_PG"
      "TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE"
      "TOFU_STORAGE_CONNECTION_FILE"
      "TOFU_STORAGE_MODE"
      "TOFU_STORAGE_PARENT_PID"
      "TOFU_STORAGE_PROJECT_ROOT"
      "TOFU_STORAGE_TEST_BACKEND"
      "TOFU_STORAGE_TEST_POSTGRES_DSN_FILE"
      "TOFU_STORAGE_TOKEN"
-}}
{{- $seen := dict -}}
{{- range $index, $entry := .Values.extraEnv -}}
{{- $name := required (printf "extraEnv[%d].name is required" $index) $entry.name -}}
{{- if has $name $protected -}}
{{- fail (printf "extraEnv[%d].name %q is managed by the chart and cannot be overridden" $index $name) -}}
{{- end -}}
{{- if hasKey $seen $name -}}
{{- fail (printf "extraEnv contains duplicate name %q" $name) -}}
{{- end -}}
{{- $_ := set $seen $name true -}}
{{- end -}}
{{- end }}

{{- define "tofu.commonEnv" -}}
- name: TOFU_DEPLOYMENT_MODE
  value: distributed
- name: TOFU_DISTRIBUTED_PREVIEW_MODE
  value: read-only
- name: MALLOC_ARENA_MAX
  value: "8"
- name: TOFU_PROCESS_ROLE
  value: {{ .role | quote }}
- name: TOFU_REPLICA_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.uid
- name: TOFU_POSTGRES_DSN_FILE
  value: /run/secrets/tofu/postgres-dsn
- name: TOFU_REDIS_URL_FILE
  value: /run/secrets/tofu/redis-url
- name: TOFU_AUTH_MODE
  value: {{ .root.Values.authMode | quote }}
{{- end }}

{{- define "tofu.appEnv" -}}
{{ include "tofu.commonEnv" . }}
- name: TOFU_STORAGE_CONNECTION_FILE
  value: /run/tofu-storage/connection.json
{{- with .root.Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "tofu.sidecarEnv" -}}
{{ include "tofu.commonEnv" . }}
- name: TOFU_STORAGE_CONNECTION_FILE
  value: /run/tofu-storage/connection.json
{{- end }}

{{- define "tofu.secretVolume" -}}
- name: external-services
  secret:
    secretName: {{ .Values.secrets.existingSecret | quote }}
    defaultMode: 0400
    items:
      - key: {{ .Values.secrets.postgresDsnKey | quote }}
        path: postgres-dsn
      - key: {{ .Values.secrets.redisUrlKey | quote }}
        path: redis-url
{{- end }}

{{- define "tofu.runtimeVolumes" -}}
{{ include "tofu.secretVolume" . }}
- name: storage-connection
  emptyDir:
    medium: Memory
    sizeLimit: 1Mi
- name: runtime-data
  emptyDir: {}
- name: runtime-logs
  emptyDir: {}
- name: runtime-uploads
  emptyDir: {}
- name: runtime-tmp
  emptyDir: {}
{{- end }}

{{- define "tofu.appVolumeMounts" -}}
- {name: external-services, mountPath: /run/secrets/tofu, readOnly: true}
- {name: storage-connection, mountPath: /run/tofu-storage}
- {name: runtime-data, mountPath: /app/data}
- {name: runtime-logs, mountPath: /app/logs}
- {name: runtime-uploads, mountPath: /app/uploads}
- {name: runtime-tmp, mountPath: /tmp}
{{- end }}

{{- define "tofu.sidecarVolumeMounts" -}}
- {name: external-services, mountPath: /run/secrets/tofu, readOnly: true}
- {name: storage-connection, mountPath: /run/tofu-storage}
- {name: runtime-data, mountPath: /app/data}
- {name: runtime-logs, mountPath: /app/logs}
- {name: runtime-tmp, mountPath: /tmp}
{{- end }}

{{- define "tofu.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "tofu.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "tofu.storageSidecar" -}}
- name: storage-sidecar
  image: {{ include "tofu.apiImage" .root }}
  imagePullPolicy: {{ .root.Values.images.pullPolicy }}
  command: ["python", "-m", "lib.storage_sidecar"]
  env:
{{ include "tofu.sidecarEnv" . | indent 4 }}
  resources:
{{ toYaml .root.Values.sidecar.resources | indent 4 }}
  securityContext:
{{ include "tofu.containerSecurityContext" . | indent 4 }}
  volumeMounts:
{{ include "tofu.sidecarVolumeMounts" .root | indent 4 }}
  startupProbe:
    exec:
      command: ["python", "-m", "lib.storage.connection_probe"]
    periodSeconds: 2
    failureThreshold: 30
  readinessProbe:
    exec:
      command: ["python", "-m", "lib.storage.connection_probe"]
    periodSeconds: 5
    failureThreshold: 2
  livenessProbe:
    exec:
      command: ["python", "-m", "lib.storage.connection_probe", "--liveness"]
    periodSeconds: 15
    failureThreshold: 3
{{- end }}
