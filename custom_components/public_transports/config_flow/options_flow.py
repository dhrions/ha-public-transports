"""The options flow: re-choose line/direction filters, scan interval, quiet hours, and
walking-time offsets on an existing entry.
"""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import BooleanSelector, SelectOptionDict, TimeSelector

from ..const import (
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_WALKING_TIME_MINUTES,
    MIN_SCAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_OPTIONS,
)
from ..coordinator import (
    _clamp_walking_time,
    entry_quiet_hours,
    entry_scan_interval,
    entry_sense_specs,
    entry_stop_code_count,
    entry_walking_time,
    estimate_daily_calls,
)
from .destinations import destination_options, destinations_from_calls, specs_from_destination_choice
from .helpers import (
    ALL_DIRECTIONS,
    ALL_LINES,
    _directions_from_calls,
    _lines_from_calls,
    _walking_time_selector,
    dropdown,
    probe_available_passages,
    resolve_line_names,
)

_LOGGER = logging.getLogger(__name__)


class PublicTransportsOptionsFlowHandler(config_entries.OptionsFlow):
    """Let the user re-choose the line/direction filter after setup.

    Mono-sens entry : ligne + sens éditables. Entrée « 2 sens » (2 specs) : seule la ligne
    est éditable (appliquée aux 2 sens) — le partage en 2 sens reste tel quel.
    """

    def __init__(self, config_entry):
        """Initialize options flow.

        Writes the private `_config_entry` the base class reads, not the public
        `config_entry` property: recent HA core dropped its setter entirely (still
        present-but-deprecated in this repo's pinned test dependency), so assigning
        through the property crashes in production while local tests stay green.
        """
        self._config_entry = config_entry
        self._probe_rate_limit = None
        # Options en attente entre l'étape init et l'étape avancée (offset par capteur) :
        # l'étape avancée les complète avec le walking_time propre à chaque spec.
        self._pending_options = None
        # État en attente entre l'étape init et l'éventuel raffinement par terminus
        # (async_step_select_destination), pour une entrée mono-sens dont le sens choisi
        # forke en plusieurs terminus.
        self._pending_destination = None

    @staticmethod
    def _format_count(value: int) -> str:
        """1234567 -> "1 234 567" (espace insécable évitée : rendu HA simple)."""
        return f"{value:,}".replace(",", " ")

    def _estimate_placeholders(self, scan_interval: int, quiet_start: str | None, quiet_end: str | None):
        """Build the description_placeholders shown on the form (and reused by the
        quota_exceeded error, which HA substitutes with the same dict).
        """
        code_count = entry_stop_code_count(self.config_entry)
        estimate = estimate_daily_calls(scan_interval, code_count, quiet_start, quiet_end)
        limit_day = self._probe_rate_limit.limit_day if self._probe_rate_limit else None
        limit_text = (
            f" sur {self._format_count(limit_day)} disponibles"
            if limit_day is not None
            else " (quota inconnu pour ce producteur)"
        )
        return {
            "estimate": self._format_count(estimate),
            "limit_text": limit_text,
            # Repris par les erreurs scan_interval_out_of_range / invalid_walking_time,
            # jamais affichés hors erreur — inoffensifs à calculer systématiquement (pas de
            # coût, pas d'appel réseau).
            "min_scan_interval": str(MIN_SCAN_INTERVAL_SECONDS),
            "max_scan_interval": str(MAX_SCAN_INTERVAL_SECONDS),
            "max_walking_time": str(MAX_WALKING_TIME_MINUTES),
        }, estimate, limit_day

    async def async_step_init(self, user_input=None):
        """Re-probe the stop and edit the line/sens/fréquence/créneaux de silence."""
        data = self.config_entry.data
        specs = entry_sense_specs(self.config_entry)
        primary = specs[0]
        multi_sense = len(specs) > 1
        # Un entry multi-sens peut être soit « les deux sens d'UNE ligne » (2 specs, même
        # line_filter — le cas historique que ce formulaire édite), soit « une ligne par
        # capteur » sur un pôle (N specs, line_filter différent par spec). Les deux se
        # distinguent uniquement par ce test : appliquer le choix de ligne du formulaire à
        # toutes les specs, comme le fait la branche multi_sense ci-dessous, écraserait le
        # filtre propre à chaque ligne dans le second cas — bug réel du 2026-08-23, où une
        # simple validation du formulaire (même sans changer "Ligne") a rétamé les 9 capteurs
        # d'un pôle en les filtrant tous sur la même ligne.
        split_by_line = multi_sense and len({s.get("line_filter") for s in specs}) > 1

        try:
            calls, self._probe_rate_limit = await probe_available_passages(
                self.hass, data["transit_company"], data.get("api_token"), primary.get("stop_code")
            )
        except Exception as err:
            _LOGGER.error(f"Error probing stop in options flow: {err}")
            calls, self._probe_rate_limit = [], None

        lines = await resolve_line_names(self.hass, _lines_from_calls(calls))
        line_options = {ALL_LINES: "Toutes les lignes", **lines}
        cur_line = primary.get("line_filter") or ALL_LINES
        if cur_line != ALL_LINES and cur_line not in line_options:
            line_options[cur_line] = primary.get("line_name") or cur_line

        senses = _directions_from_calls(calls, primary.get("line_filter"))
        dir_options = {ALL_DIRECTIONS: "Tous les sens", **senses}
        cur_dir = primary.get("direction_filter") or ALL_DIRECTIONS
        if cur_dir != ALL_DIRECTIONS and cur_dir not in dir_options:
            dir_options[cur_dir] = primary.get("direction_label") or cur_dir

        cur_scan_interval = int(entry_scan_interval(self.config_entry).total_seconds())
        scan_interval_options = dict(SCAN_INTERVAL_OPTIONS)
        if cur_scan_interval not in scan_interval_options:
            scan_interval_options[cur_scan_interval] = f"{cur_scan_interval} secondes"

        # entry_quiet_hours renvoie le créneau nuit par défaut si l'entrée n'en a jamais
        # défini — le formulaire le pré-remplit donc, et l'utilisateur peut l'effacer en
        # mettant début == fin (sonde en continu).
        cur_quiet_start, cur_quiet_end = entry_quiet_hours(self.config_entry)
        cur_walking_time = entry_walking_time(self.config_entry)

        if user_input is not None:
            raw_scan_interval = user_input.get("scan_interval", cur_scan_interval)
            try:
                scan_interval = int(raw_scan_interval)
            except (TypeError, ValueError):
                scan_interval = None

            raw_walking_time = user_input.get("walking_time", cur_walking_time)
            try:
                walking_time = int(raw_walking_time)
            except (TypeError, ValueError):
                walking_time = None

            quiet_start = (user_input.get("quiet_hours_start") or "").strip() or None
            quiet_end = (user_input.get("quiet_hours_end") or "").strip() or None
            errors = {}
            if scan_interval is None:
                errors["base"] = "invalid_scan_interval"
            elif not (MIN_SCAN_INTERVAL_SECONDS <= scan_interval <= MAX_SCAN_INTERVAL_SECONDS):
                errors["base"] = "scan_interval_out_of_range"
            elif walking_time is None or not (0 <= walking_time <= MAX_WALKING_TIME_MINUTES):
                errors["base"] = "invalid_walking_time"
            elif bool(quiet_start) != bool(quiet_end):
                errors["base"] = "quiet_hours_incomplete"

            # scan_interval est requis par _estimate_placeholders même quand la saisie est
            # invalide (pour réafficher un formulaire cohérent) — la valeur précédente sert
            # de repli, l'erreur ci-dessus empêche déjà la sauvegarde.
            placeholders, estimate, limit_day = self._estimate_placeholders(
                scan_interval if scan_interval is not None else cur_scan_interval,
                quiet_start, quiet_end
            )
            if not errors and limit_day is not None and estimate > limit_day:
                errors["base"] = "quota_exceeded"

            if errors:
                # raw_scan_interval (pas scan_interval, potentiellement None si le parsing
                # a échoué) : l'utilisateur doit revoir exactement ce qu'il a tapé pour le
                # corriger, pas un champ retombé sur l'ancienne valeur ou sur "None".
                schema = self._build_schema(
                    line_options, cur_line, dir_options, cur_dir, multi_sense, split_by_line,
                    scan_interval_options, raw_scan_interval, quiet_start or "", quiet_end or "",
                    raw_walking_time,
                )
                return self.async_show_form(
                    step_id="init", data_schema=schema, errors=errors, description_placeholders=placeholders
                )

            if split_by_line:
                # Pas de champ "line" dans le schéma pour ce cas (cf. _build_schema) —
                # chaque spec garde son propre line_filter/line_name, seuls
                # scan_interval/quiet_hours sont éditables ici.
                new_specs = specs
            else:
                line = user_input.get("line")
                line_filter = None if not line or line == ALL_LINES else line
                line_name = line_options.get(line) if line_filter else None
                if multi_sense:
                    # ligne appliquée aux 2 specs (les deux sens d'UNE même ligne), sens conservés
                    new_specs = [
                        {**spec, "line_filter": line_filter, "line_name": line_name}
                        for spec in specs
                    ]
                else:
                    direction = user_input.get("direction")
                    direction_filter = None if not direction or direction == ALL_DIRECTIONS else direction
                    direction_label = dir_options.get(direction) if direction_filter else None

                    # Un sens mono-capteur peut lui-même forker en plusieurs terminus (ex.
                    # métro 13 nord) — même raffinement optionnel qu'à la création (cf.
                    # flow.py), proposé ici seulement quand ce fork existe réellement.
                    # Une entrée déjà scindée par terminus (multi_sense, même line_filter
                    # partout) repasse par la branche multi_sense ci-dessus à sa prochaine
                    # ouverture des Options — chaque spec y garde son propre
                    # destination_filter, seule la ligne reste éditable pour toutes à la fois.
                    destinations = destinations_from_calls(calls, line_filter, direction_filter)
                    if len(destinations) > 1:
                        self._pending_destination = {
                            "stop_code": primary.get("stop_code"),
                            "stop_codes": primary.get("stop_codes"),
                            "line_filter": line_filter,
                            "line_name": line_name,
                            "direction_filter": direction_filter,
                            "direction_label": direction_label,
                            "destinations": destinations,
                            "options_data": {
                                "scan_interval": scan_interval,
                                "quiet_hours_start": quiet_start,
                                "quiet_hours_end": quiet_end,
                                "walking_time": walking_time,
                            },
                        }
                        return await self.async_step_select_destination()

                    new_specs = [{
                        k: v for k, v in {
                            **primary,
                            "line_filter": line_filter,
                            "line_name": line_name,
                            "direction_filter": direction_filter,
                            "direction_label": direction_label,
                        }.items()
                        # Une entrée qui avait un destination_filter (le sens ne forke plus
                        # au sondage courant, ex. service dégradé revenu à la normale)
                        # retombe sur le capteur agrégé plutôt que de garder un filtre
                        # devenu orphelin.
                        if k not in ("destination_filter", "destination_label")
                    }]

            options_data = {
                "senses": new_specs,
                "scan_interval": scan_interval,
                "quiet_hours_start": quiet_start,
                "quiet_hours_end": quiet_end,
                "walking_time": walking_time,
            }
            # Étape avancée facultative : régler l'offset par capteur (ligne × sens),
            # proposée seulement quand il y a plusieurs capteurs (sinon l'override par sens
            # équivaudrait au défaut d'entrée). new_specs est passé à l'étape suivante, qui
            # y greffe le walking_time propre à chaque capteur avant de créer l'entrée.
            if multi_sense and user_input.get("configure_per_sense"):
                self._pending_options = options_data
                return await self.async_step_walking_advanced()
            return self.async_create_entry(title="", data=options_data)

        schema = self._build_schema(
            line_options, cur_line, dir_options, cur_dir, multi_sense, split_by_line,
            scan_interval_options, cur_scan_interval, cur_quiet_start, cur_quiet_end,
            cur_walking_time,
        )
        placeholders, _estimate, _limit_day = self._estimate_placeholders(
            cur_scan_interval, cur_quiet_start or None, cur_quiet_end or None
        )
        return self.async_show_form(step_id="init", data_schema=schema, description_placeholders=placeholders)

    @staticmethod
    def _build_schema(
        line_options, cur_line, dir_options, cur_dir, multi_sense, split_by_line,
        scan_interval_options, cur_scan_interval, cur_quiet_start, cur_quiet_end,
        cur_walking_time,
    ):
        """Assemble the options form schema — shared by the first render and any
        error re-render, so the user's already-typed values survive a rejection.

        split_by_line (une entrée « un capteur par ligne » sur un pôle) n'a pas de champ
        "line" éditable : chaque spec a délibérément sa propre ligne, et un unique
        sélecteur ne peut représenter — ni écraser sans casse — cette diversité. Cf.
        async_step_init pour l'incident que ça a causé le 2026-08-23.

        walking_time (défaut d'entrée) est toujours présent. configure_per_sense (bascule
        vers l'étape avancée par capteur) n'apparaît que sur une entrée multi-capteurs :
        sur un capteur unique, un offset par sens ne dirait rien de plus que ce défaut.
        """
        schema = {}
        if not split_by_line:
            schema[vol.Required("line", default=cur_line)] = dropdown(line_options)
        if not multi_sense:
            schema[vol.Required("direction", default=cur_dir)] = dropdown(dir_options)
        # default=str(...) : dropdown() stringifie ses clés d'options (SelectOptionDict
        # exige value=str) ; un champ non touché par l'utilisateur renvoie ce default tel
        # quel à la soumission — un int ici ferait échouer la validation avec "expected
        # str", peu importe si les créneaux horaires sont eux corrects.
        schema[vol.Required("scan_interval", default=str(cur_scan_interval))] = dropdown(
            scan_interval_options, custom_value=True
        )
        schema[vol.Optional("walking_time", default=cur_walking_time)] = _walking_time_selector()
        # Pas de default="" : TimeSelector rejette la chaîne vide à la validation (les
        # deux champs doivent rester réellement absents tant qu'aucun créneau n'est
        # configuré, pas "vides").
        start_key = vol.Optional("quiet_hours_start", default=cur_quiet_start) if cur_quiet_start else vol.Optional("quiet_hours_start")
        end_key = vol.Optional("quiet_hours_end", default=cur_quiet_end) if cur_quiet_end else vol.Optional("quiet_hours_end")
        # vol.Any(None, TimeSelector()) : le bouton "X" du TimeSelector envoie une valeur
        # explicite `null` (pas une absence de clé), donc vol.Optional seul ne protège pas
        # — TimeSelector() valide en `str` en interne et rejette None avec "expected str".
        schema[start_key] = vol.Any(None, TimeSelector())
        schema[end_key] = vol.Any(None, TimeSelector())
        if multi_sense:
            schema[vol.Optional("configure_per_sense", default=False)] = BooleanSelector()
        return vol.Schema(schema)

    async def async_step_select_destination(self, user_input=None):
        """Refine the single sense just chosen (async_step_init) by terminus, when it
        forks into ≥2 — the Options-flow counterpart of flow.py's step of the same name.

        Multi-select, every option checked by default (tout coché) — same rationale as
        the initial config flow: filtering here costs no extra API call (same shared raw
        feed), so the user unchecks what they don't want rather than having to opt in.
        """
        pending = self._pending_destination
        destinations = pending["destinations"]
        options = destination_options(destinations)

        def spec_builder(destination_filter, destination_label):
            spec = {
                "stop_code": pending["stop_code"],
                "line_filter": pending["line_filter"],
                "line_name": pending["line_name"],
                "direction_filter": pending["direction_filter"],
                "direction_label": pending["direction_label"],
            }
            if pending.get("stop_codes"):
                spec["stop_codes"] = pending["stop_codes"]
            if destination_filter:
                spec["destination_filter"] = destination_filter
                spec["destination_label"] = destination_label
            return spec

        if user_input is not None:
            chosen = [key for key in (user_input.get("destinations") or []) if key in options]
            new_specs = specs_from_destination_choice(spec_builder, destinations, chosen)
            options_data = {**pending["options_data"], "senses": new_specs}
            return self.async_create_entry(title="", data=options_data)

        return self.async_show_form(
            step_id="select_destination",
            data_schema=vol.Schema({
                vol.Required("destinations", default=list(options)): dropdown(options, multiple=True)
            }),
        )

    async def async_step_walking_advanced(self, user_input=None):
        """Advanced step: a per-sensor (line × sense) walking-time override.

        Reached from async_step_init only when the user ticked "configure_per_sense" on a
        multi-sensor entry. Each field is optional: left empty, the sensor inherits the
        entry-level walking_time (the spec carries no walking_time key); filled, it overrides
        just that sensor — a platform/direction a few minutes closer or farther.

        Field keys are the sensors' own display labels (disambiguated for uniqueness) rather
        than opaque indices, so HA renders a legible label per line/sense without needing a
        translation for a key it can't know in advance.
        """
        options_data = self._pending_options
        specs = options_data["senses"]
        labels = self._spec_labels(specs)

        if user_input is not None:
            new_specs = []
            for spec, label in zip(specs, labels):
                spec = {k: v for k, v in spec.items() if k != "walking_time"}
                override = _clamp_walking_time(user_input.get(label))
                if override is not None:
                    spec["walking_time"] = override
                new_specs.append(spec)
            return self.async_create_entry(title="", data={**options_data, "senses": new_specs})

        schema = {}
        for spec, label in zip(specs, labels):
            cur = _clamp_walking_time(spec.get("walking_time"))
            key = vol.Optional(label, default=cur) if cur is not None else vol.Optional(label)
            schema[key] = _walking_time_selector()
        return self.async_show_form(
            step_id="walking_advanced",
            data_schema=vol.Schema(schema),
            description_placeholders={"default_walking_time": str(options_data.get("walking_time") or 0)},
        )

    @staticmethod
    def _spec_labels(specs):
        """One legible, unique label per spec (line + sense), used as the advanced-step
        field keys. Falls back to "Capteur N" and appends " (N)" to break any collision so
        two same-named senses stay distinct form fields."""
        labels, seen = [], {}
        for index, spec in enumerate(specs):
            parts = []
            if spec.get("line_name"):
                parts.append(str(spec["line_name"]))
            destination_or_direction = spec.get("destination_label") or spec.get("direction_label")
            if destination_or_direction:
                parts.append(f"→ {destination_or_direction}")
            label = " ".join(parts) or f"Capteur {index + 1}"
            if label in seen:
                seen[label] += 1
                label = f"{label} ({seen[label]})"
            else:
                seen[label] = 1
            labels.append(label)
        return labels
