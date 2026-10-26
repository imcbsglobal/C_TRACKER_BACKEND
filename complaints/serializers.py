from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers
from rest_framework.authtoken.models import Token

from .models import (
    ACCESS_CHOICES,
    Attachment,
    Category,
    Complaint,
    ComplaintSeen,
    ComplaintTask,
    TaskFollowup,
    Followup,
    Notification,
    Priority,
    Project,
    Section,
    Sprint,
    PROTECTED_STATUSES,
    Status,
    TeamMember,
    ensure_core_statuses,
    get_stored_password,
    save_stored_password,
    seed_defaults,
)

User = get_user_model()

DEV_KINDS = ["task", "bug"]
ALL_KINDS = ["complaint", "task", "bug"]


def user_access(user):
    """"support", "developer" or "both". Admin always has both."""
    if user.is_superuser:
        return "both"
    if not hasattr(user, "_access_cache"):
        member = TeamMember.objects.filter(user=user).first()
        user._access_cache = member.access if member else "developer"
    return user._access_cache


def allowed_kinds(user):
    """The task types this login may see and create."""
    if user.is_superuser:
        return ALL_KINDS
    return {"support": ["complaint"], "developer": DEV_KINDS}.get(user_access(user), ALL_KINDS)


def can_edit_complaint(user, complaint):
    """Support cases are visible to the whole support team, but only "own" ones can be
    edited: assigned to you, you are a helper, or you filed it. The admin edits all.
    Tasks and bugs are not affected."""
    if user.is_superuser or complaint.kind != "complaint":
        return True
    if complaint.created_by_id == user.id:
        return True
    if complaint.assigned_to_id and complaint.assigned_to.user_id == user.id:
        return True
    return any(h.user_id == user.id for h in complaint.helpers.all())


def dev_side_members(project):
    """Developers (developer / both access, active login) on a project: the people
    told about a task support escalates to the developer team."""
    if project is None:
        return []
    return list(
        project.members.filter(
            access__in=("developer", "both"), user__isnull=False, user__is_active=True
        ).select_related("user")
    )


def default_kind(user):
    return "complaint" if user_access(user) == "support" else "task"


def display_name(user):
    full = f"{user.first_name} {user.last_name}".strip()
    return full or user.email or user.username


def ensure_member(user):
    """Give every non-admin login a matching team member so they can be assigned work."""
    if user.is_superuser:
        return None

    member = TeamMember.objects.filter(user=user).first()
    if member is None and user.email:
        member = TeamMember.objects.filter(
            user__isnull=True, email__iexact=user.email
        ).first()
    if member is None:
        member = TeamMember()

    member.user = user
    member.first_name = user.first_name or (user.email or user.username).split("@")[0]
    member.last_name = user.last_name
    member.email = user.email
    member.save()
    return member


def default_choice(model, preferred, field="value"):
    """The value new tasks get when none is sent: the classic default if it still
    exists, otherwise the first item in the admin's list."""
    seed_defaults()
    if model.objects.filter(**{field: preferred}).exists():
        return preferred
    first = model.objects.first()
    return getattr(first, field) if first else preferred


def make_value(model, label, max_len):
    """Permanent slug for a new status/priority, unique within the table."""
    base = slugify(label).replace("-", "_")[:max_len] or "item"
    value, n = base, 2
    while model.objects.filter(value=value).exists():
        suffix = f"_{n}"
        value = base[: max_len - len(suffix)] + suffix
        n += 1
    return value


class TeamMemberSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    open_tasks = serializers.IntegerField(read_only=True)

    class Meta:
        model = TeamMember
        fields = [
            "id",
            "first_name",
            "last_name",
            "email",
            "access",
            "full_name",
            "open_tasks",
        ]


class AttachmentSerializer(serializers.ModelSerializer):
    can_delete = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = ["id", "url", "name", "size", "created_at", "can_delete"]
        read_only_fields = fields

    def get_can_delete(self, obj):
        request = self.context.get("request")
        return bool(
            request
            and (request.user.is_superuser or obj.uploaded_by_id == request.user.id)
        )


class FollowupSerializer(serializers.ModelSerializer):
    attachments = AttachmentSerializer(many=True, read_only=True)
    reactions = serializers.SerializerMethodField()

    class Meta:
        model = Followup
        fields = ["id", "note", "created_by_name", "created_at", "attachments", "reactions"]
        read_only_fields = ["created_by_name", "created_at"]

    def get_reactions(self, obj):
        """Grouped per emoji: [{emoji, count, mine, users}], in the order first used."""
        request = self.context.get("request")
        uid = request.user.id if request else None
        groups = {}
        for r in obj.reactions.all():  # prefetched by the views
            g = groups.setdefault(
                r.emoji, {"emoji": r.emoji, "count": 0, "mine": False, "users": []}
            )
            g["count"] += 1
            g["mine"] = g["mine"] or r.user_id == uid
            g["users"].append(display_name(r.user))
        return list(groups.values())


class TaskFollowupSerializer(serializers.ModelSerializer):
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = TaskFollowup
        fields = ["id", "task", "note", "created_by_name", "created_at", "can_manage"]
        read_only_fields = ["created_by_name", "created_at"]

    def get_can_manage(self, obj):
        request = self.context.get("request")
        return bool(
            request and (request.user.is_superuser or obj.created_by_id == request.user.id)
        )

    def validate_note(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Write something first.")
        return value

    def validate(self, attrs):
        if self.instance is not None and "task" in attrs and attrs["task"].id != self.instance.task_id:
            raise serializers.ValidationError({"task": "A follow-up can't move to another task."})
        return attrs


class ComplaintTaskSerializer(serializers.ModelSerializer):
    """A task (name + goal) on a support complaint."""

    is_closed = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    followups = TaskFollowupSerializer(many=True, read_only=True)
    # Escalation: where the task went, and (for the developer team) how that work is going.
    dev_task_status = serializers.SerializerMethodField()
    dev_task_status_label = serializers.SerializerMethodField()
    dev_task_is_closed = serializers.SerializerMethodField()
    dev_task_assignee_name = serializers.SerializerMethodField()

    class Meta:
        model = ComplaintTask
        fields = [
            "id",
            "complaint",
            "name",
            "goal",
            "status",
            "is_closed",
            "can_edit",
            "followups",
            "escalation",
            "escalated_at",
            "escalated_by_name",
            "escalation_note",
            "dev_task_status",
            "dev_task_status_label",
            "dev_task_is_closed",
            "dev_task_assignee_name",
            "created_by_name",
            "created_at",
            "updated_at",
            "closed_at",
        ]
        read_only_fields = [
            "created_by_name",
            "created_at",
            "updated_at",
            "closed_at",
            "escalation",  # changed only through POST /complaint-tasks/<id>/escalate/
            "escalated_at",
            "escalated_by_name",
            "escalation_note",
        ]

    def _closed_values(self):
        if "_closed_values" not in self.context:
            self.context["_closed_values"] = set(
                Status.objects.filter(is_closed=True).values_list("value", flat=True)
            )
        return self.context["_closed_values"]

    def _status_labels(self):
        if "_status_labels" not in self.context:
            self.context["_status_labels"] = dict(Status.objects.values_list("value", "label"))
        return self.context["_status_labels"]

    def get_is_closed(self, obj):
        return obj.status in self._closed_values()

    def get_dev_task_status(self, obj):
        return obj.dev_task.status if obj.dev_task_id else None

    def get_dev_task_status_label(self, obj):
        if not obj.dev_task_id:
            return None
        value = obj.dev_task.status
        return self._status_labels().get(value) or value.replace("_", " ").title()

    def get_dev_task_is_closed(self, obj):
        return bool(obj.dev_task_id) and obj.dev_task.status in self._closed_values()

    def get_dev_task_assignee_name(self, obj):
        dev = obj.dev_task if obj.dev_task_id else None
        return dev.assigned_to.full_name if dev and dev.assigned_to else None

    def get_can_edit(self, obj):
        request = self.context.get("request")
        return True if request is None else can_edit_complaint(request.user, obj.complaint)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Task name is required.")
        return value

    def validate_goal(self, value):
        return value.strip()

    def validate_status(self, value):
        if not Status.objects.filter(value=value).exists():
            raise serializers.ValidationError("Unknown status.")
        return value

    def validate(self, attrs):
        request = self.context["request"]
        if self.instance is None:
            complaint = attrs.get("complaint")
            if complaint is None:
                raise serializers.ValidationError({"complaint": "Choose a complaint."})
            if complaint.kind != "complaint":
                raise serializers.ValidationError(
                    {"complaint": "Tasks can only be added to support complaints."}
                )
            if not can_edit_complaint(request.user, complaint):
                raise serializers.ValidationError(
                    {"complaint": "Only the assignee, helpers or the person who filed this complaint can add tasks."}
                )
            attrs.setdefault("status", default_choice(Status, "open"))
        elif "complaint" in attrs and attrs["complaint"].id != self.instance.complaint_id:
            raise serializers.ValidationError({"complaint": "A task can't move to another complaint."})
        return attrs


class ComplaintSerializer(serializers.ModelSerializer):
    followups = FollowupSerializer(many=True, read_only=True)
    attachments = serializers.SerializerMethodField()  # images added when the case was filed
    assigned_to_name = serializers.SerializerMethodField()
    helpers = serializers.PrimaryKeyRelatedField(
        many=True, queryset=TeamMember.objects.all(), required=False
    )
    helpers_names = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    sprint_name = serializers.SerializerMethodField()
    section_name = serializers.SerializerMethodField()
    task_number = serializers.SerializerMethodField()
    urgent = serializers.SerializerMethodField()
    case_number = serializers.SerializerMethodField()
    escalated_to_name = serializers.SerializerMethodField()
    escalated_to_me = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    is_closed = serializers.SerializerMethodField()
    # Support complaints: the tasks added to it, and the numbers behind its progress ring.
    tasks = ComplaintTaskSerializer(many=True, read_only=True)
    tasks_total = serializers.SerializerMethodField()
    tasks_done = serializers.SerializerMethodField()
    tasks_in_progress = serializers.SerializerMethodField()
    escalation_options = serializers.SerializerMethodField()
    escalated_from = serializers.SerializerMethodField()

    class Meta:
        model = Complaint
        fields = [
            "id",
            "task_number",
            "case_number",
            "urgent",
            "kind",
            "title",
            "description",
            "customer",
            "phone",
            "category",
            "product_type",
            "product_name",
            "escalated_to",
            "escalated_to_name",
            "escalated_to_me",
            "can_edit",
            "is_mine",
            "is_closed",
            "tasks",
            "tasks_total",
            "tasks_done",
            "tasks_in_progress",
            "escalation_options",
            "escalated_from",
            "priority",
            "status",
            "project",
            "project_name",
            "section",
            "section_name",
            "sprint",
            "sprint_name",
            "assigned_to",
            "assigned_to_name",
            "helpers",
            "helpers_names",
            "due_date",
            "created_by_name",
            "followups",
            "attachments",
            "created_at",
            "updated_at",
            "closed_at",
        ]
        read_only_fields = [
            "created_by_name",
            "created_at",
            "updated_at",
            "closed_at",
            "escalated_to",  # changed only through POST /complaints/<id>/escalate/
        ]

    def get_escalation_options(self, obj):
        """For the "Escalate task" dialog (single-case view only): is there a development
        project with the same name, and how many developers would be told?"""
        view = self.context.get("view")
        if obj.kind != "complaint" or obj.project_id is None:
            return None
        if getattr(view, "action", None) != "retrieve":
            return None
        twin = obj.project.twin()
        return {
            "dev_project": twin.name if twin else None,
            "developers": len(dev_side_members(twin)) if twin else 0,
        }

    def get_escalated_from(self, obj):
        """A task / bug the support team escalated to the developers: who handed it up,
        from which support case, and when. None for every other task."""
        link = next(iter(obj.escalated_from_tasks.all()), None)
        if link is None:
            return None
        case = link.complaint
        return {
            "by": link.escalated_by_name,
            "at": link.escalated_at,
            "note": link.escalation_note,
            "case_id": case.id,
            "case_number": case.case_number,
            "support_task": link.name,
        }

    def get_case_number(self, obj):
        return obj.case_number

    def get_attachments(self, obj):
        items = [a for a in obj.attachments.all() if a.followup_id is None]
        return AttachmentSerializer(items, many=True, context=self.context).data

    def get_escalated_to_name(self, obj):
        return obj.escalated_to.full_name if obj.escalated_to_id else None

    def get_escalated_to_me(self, obj):
        request = self.context.get("request")
        return bool(
            request and obj.escalated_to_id and obj.escalated_to.user_id == request.user.id
        )

    def get_can_edit(self, obj):
        request = self.context.get("request")
        return True if request is None else can_edit_complaint(request.user, obj)

    def get_is_mine(self, obj):
        request = self.context.get("request")
        if request is None:
            return False
        uid = request.user.id
        if obj.assigned_to_id and obj.assigned_to.user_id == uid:
            return True
        return any(h.user_id == uid for h in obj.helpers.all())

    def get_is_closed(self, obj):
        if "_closed_values" not in self.context:
            self.context["_closed_values"] = set(
                Status.objects.filter(is_closed=True).values_list("value", flat=True)
            )
        return obj.status in self.context["_closed_values"]

    def _closed_set(self):
        if "_closed_values" not in self.context:
            self.context["_closed_values"] = set(
                Status.objects.filter(is_closed=True).values_list("value", flat=True)
            )
        return self.context["_closed_values"]

    def get_tasks_total(self, obj):
        return len(obj.tasks.all())  # prefetched by the views

    def get_tasks_done(self, obj):
        closed = self._closed_set()
        return sum(1 for t in obj.tasks.all() if t.status in closed)

    def get_tasks_in_progress(self, obj):
        closed = self._closed_set()
        return sum(1 for t in obj.tasks.all() if t.status not in closed and t.status != "open")

    def validate_product_type(self, value):
        # The case ID prefix (OP / CP) comes from this, so it is fixed once created.
        if self.instance and value != self.instance.product_type:
            raise serializers.ValidationError(
                "Product type can't change after the case is created."
            )
        return value

    def get_project_name(self, obj):
        return obj.project.name if obj.project else None

    def get_sprint_name(self, obj):
        return obj.sprint.name if obj.sprint_id else None

    def get_section_name(self, obj):
        return obj.section.name if obj.section_id else None

    def validate_kind(self, value):
        if value not in allowed_kinds(self.context["request"].user):
            raise serializers.ValidationError("You can't use that type.")
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request and user_access(request.user) == "developer":
            data.pop("customer", None)  # customer details never reach developers
            data.pop("phone", None)
        return data

    def validate_project(self, project):
        user = self.context["request"].user
        if not user.is_superuser and not project.members.filter(user=user).exists():
            raise serializers.ValidationError("You are not a member of that project.")
        return project

    def validate_status(self, value):
        if not Status.objects.filter(value=value).exists():
            raise serializers.ValidationError("Unknown status.")
        return value

    def validate_priority(self, value):
        if not Priority.objects.filter(value=value).exists():
            raise serializers.ValidationError("Unknown priority.")
        return value

    def validate_category(self, value):
        legacy = self.instance and self.instance.category == value
        if not legacy and not Category.objects.filter(name=value).exists():
            raise serializers.ValidationError("Unknown category.")
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        creating = self.instance is None
        if creating:  # fill anything not sent
            if "kind" not in attrs:
                # No type sent: follow the project (support project -> complaint).
                chosen = attrs.get("project")
                if chosen is not None:
                    attrs["kind"] = "complaint" if chosen.kind == "support" else "task"
                else:
                    attrs["kind"] = default_kind(user)
                if attrs["kind"] not in allowed_kinds(user):
                    raise serializers.ValidationError({"kind": "You can't use that type."})
            attrs.setdefault("status", default_choice(Status, "open"))
            attrs.setdefault("priority", default_choice(Priority, "medium"))
            attrs.setdefault("category", default_choice(Category, "General", "name"))

        kind = attrs.get("kind") or self.instance.kind

        assignee = (
            attrs["assigned_to"]
            if "assigned_to" in attrs
            else (self.instance.assigned_to if self.instance else None)
        )
        if "helpers" in attrs:
            helpers = attrs["helpers"]
        elif self.instance and ("project" in attrs or "kind" in attrs):
            helpers = list(self.instance.helpers.all())
        else:
            helpers = []

        # Complaints live in support projects, tasks and bugs in development projects.
        want = "support" if kind == "complaint" else "development"

        if not attrs.get("project"):
            if creating:
                projects = (
                    Project.objects.all()
                    if user.is_superuser
                    else Project.objects.filter(members__user=user)
                )
                first = projects.filter(kind=want).order_by("id").first()
                if first is None:
                    raise serializers.ValidationError(
                        {"project": f"You are not in any {want} project yet."}
                    )
                attrs["project"] = first
            elif self.instance.project_id is None:
                raise serializers.ValidationError({"project": "Choose a project."})

        # Complaints go to support staff, tasks and bugs to developers - and the
        # person must be a member of the project.
        project = attrs.get("project") or self.instance.project
        if project.kind != want:
            raise serializers.ValidationError(
                {
                    "project": "Complaints go in a support project."
                    if kind == "complaint"
                    else "Tasks and bugs go in a development project."
                }
            )

        # Sections: tasks and bugs only, and the section must belong to the task's project.
        if "section" in attrs:
            section = attrs["section"]
        elif self.instance and self.instance.section_id:
            section = self.instance.section
        else:
            section = None
        if section is not None:
            if kind not in DEV_KINDS:
                if "section" in attrs:
                    raise serializers.ValidationError(
                        {"section": "Only tasks and bugs can sit in a section."}
                    )
                section = attrs["section"] = None
            elif section.project_id != project.id:
                if "section" in attrs:
                    raise serializers.ValidationError(
                        {"section": "That section belongs to a different project."}
                    )
                attrs["section"] = None  # moved to another project: section no longer applies

        # Sprints: tasks and bugs only, and the sprint must include the task's project.
        if "sprint" in attrs:
            sprint = attrs["sprint"]
        elif (
            self.instance
            and self.instance.sprint_id
            and not self.instance.sprint.projects.filter(id=project.id).exists()
        ):
            sprint = attrs["sprint"] = None  # moved to another project: back to backlog
        else:
            sprint = None
        if sprint is not None:
            if kind not in DEV_KINDS:
                raise serializers.ValidationError(
                    {"sprint": "Only tasks and bugs can be put in a sprint."}
                )
            if not sprint.projects.filter(id=project.id).exists():
                raise serializers.ValidationError(
                    {"sprint": "That project is not part of this sprint."}
                )
            already_in = self.instance and self.instance.sprint_id == sprint.id
            if sprint.status == "completed" and not already_in:
                raise serializers.ValidationError(
                    {"sprint": "That sprint is already completed."}
                )

        member_ids = set(project.members.values_list("id", flat=True))
        if kind == "complaint":
            side, side_msg = ("support", "both"), "Complaints can only go to support staff."
        else:
            side, side_msg = ("developer", "both"), "Tasks and bugs can only go to developers."

        if assignee:
            if assignee.id not in member_ids:
                raise serializers.ValidationError(
                    {"assigned_to": "That person is not a member of this project."}
                )
            if assignee.access not in side:
                raise serializers.ValidationError({"assigned_to": side_msg})
        for h in helpers:
            if h.id not in member_ids:
                raise serializers.ValidationError(
                    {"helpers": "Every helper must be a member of this project."}
                )
            if h.access not in side:
                raise serializers.ValidationError({"helpers": side_msg})
        return attrs

    def get_urgent(self, obj):
        """True while an admin-created task has not been opened by this (non-admin) user."""
        request = self.context.get("request")
        if request is None or request.user.is_superuser:
            return False
        if not obj.created_by_id or not obj.created_by.is_superuser:
            return False
        if "_seen_ids" not in self.context:
            self.context["_seen_ids"] = set(
                ComplaintSeen.objects.filter(user=request.user).values_list(
                    "complaint_id", flat=True
                )
            )
        return obj.id not in self.context["_seen_ids"]

    def get_task_number(self, obj):
        # Unique, permanent number made when the task is created (never reused).
        return f"#TASK{obj.id}"

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.full_name if obj.assigned_to else None

    def get_helpers_names(self, obj):
        return [m.full_name for m in obj.helpers.all()]


class UserSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, required=False, min_length=6)
    full_name = serializers.SerializerMethodField()
    is_admin = serializers.BooleanField(source="is_superuser", read_only=True)
    # Readable copy of the password the admin set. Only sent when the logged-in
    # requester is the admin (see to_representation).
    password_plain = serializers.SerializerMethodField()
    # ids of the projects this login belongs to (read), and the list to save (write)
    projects = serializers.SerializerMethodField()
    project_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Project.objects.all(), write_only=True, required=False
    )
    # support / developer / both. Write here; read back through to_representation.
    access = serializers.ChoiceField(choices=ACCESS_CHOICES, write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "is_admin",
            "is_active",
            "password",
            "password_plain",
            "projects",
            "project_ids",
            "access",
            "date_joined",
        ]
        read_only_fields = ["date_joined"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["access"] = user_access(instance)
        request = self.context.get("request")
        if not (request and request.user.is_superuser):
            data.pop("password_plain", None)  # never sent to anyone but the admin
        return data

    def get_password_plain(self, obj):
        return get_stored_password(obj)

    def _apply_access(self, user, access, project_ids):
        member = ensure_member(user)
        if member is None:  # the admin account
            return
        if access:
            member.access = access
            member.save(update_fields=["access"])
        if project_ids is not None:
            member.projects.set(project_ids)

    def get_full_name(self, obj):
        return display_name(obj)

    def get_projects(self, obj):
        member = getattr(obj, "team_member", None)
        return list(member.projects.values_list("id", flat=True)) if member else []

    def validate_email(self, value):
        value = value.strip().lower()
        qs = User.objects.filter(Q(email__iexact=value) | Q(username__iexact=value))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, attrs):
        if not self.instance and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Password is required."})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        project_ids = validated_data.pop("project_ids", None)
        access = validated_data.pop("access", None)
        user = User.objects.create_user(
            username=validated_data["email"], password=password, **validated_data
        )
        save_stored_password(user, password)
        self._apply_access(user, access, project_ids)
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        project_ids = validated_data.pop("project_ids", None)
        access = validated_data.pop("access", None)
        if "email" in validated_data:
            instance.username = validated_data["email"]
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
            save_stored_password(instance, password)
            Token.objects.filter(user=instance).delete()  # force re-login
        instance.save()
        self._apply_access(instance, access, project_ids)  # also syncs name and email
        return instance


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "complaint",
            "project",
            "message",
            "actor_name",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields


class _OptionSerializer(serializers.ModelSerializer):
    """Shared by statuses and priorities. `value` is generated once and never changes."""

    value = serializers.CharField(read_only=True)
    value_max = 30

    def validate_label(self, label):
        label = label.strip()
        if not label:
            raise serializers.ValidationError("Name is required.")
        qs = self.Meta.model.objects.filter(label__iexact=label)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That name already exists.")
        return label

    def create(self, validated_data):
        model = self.Meta.model
        validated_data["value"] = make_value(
            model, validated_data["label"], self.value_max
        )
        last = model.objects.order_by("-order").first()
        validated_data["order"] = (last.order + 1) if last else 0
        return super().create(validated_data)


class StatusSerializer(_OptionSerializer):
    value_max = 30

    class Meta:
        model = Status
        fields = ["id", "value", "label", "tone", "is_closed", "order"]
        read_only_fields = ["order"]

    def validate(self, attrs):
        # Closed is always "done"; Open is never "done". The admin can't flip these.
        if self.instance and self.instance.value in PROTECTED_STATUSES:
            attrs["is_closed"] = self.instance.value == "closed"
        return attrs

    def create(self, validated_data):
        obj = super().create(validated_data)
        ensure_core_statuses()  # slots the new status in before "Closed"
        obj.refresh_from_db()
        return obj


class PrioritySerializer(_OptionSerializer):
    value_max = 20

    class Meta:
        model = Priority
        fields = ["id", "value", "label", "tone", "order"]
        read_only_fields = ["order"]


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "order"]
        read_only_fields = ["order"]

    def validate_name(self, name):
        name = name.strip()
        if not name:
            raise serializers.ValidationError("Name is required.")
        qs = Category.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That name already exists.")
        return name

    def create(self, validated_data):
        last = Category.objects.order_by("-order").first()
        validated_data["order"] = (last.order + 1) if last else 0
        return super().create(validated_data)


class SectionOwnersMixin(serializers.Serializer):
    """owner_names + is_mine (true when the person looking is one of the owners)."""

    owner_names = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    def get_owner_names(self, obj):
        return [m.full_name for m in obj.owners.all()]

    def get_is_mine(self, obj):
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return False
        return any(m.user_id == request.user.id for m in obj.owners.all())


class SectionBriefSerializer(SectionOwnersMixin, serializers.ModelSerializer):
    owners = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Section
        fields = ["id", "name", "order", "owners", "owner_names", "is_mine"]


class ProjectSerializer(serializers.ModelSerializer):
    members = serializers.PrimaryKeyRelatedField(
        many=True, queryset=TeamMember.objects.all(), required=False
    )
    sections = SectionBriefSerializer(many=True, read_only=True)
    task_count = serializers.IntegerField(read_only=True)
    open_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "kind",
            "members",
            "sections",
            "task_count",
            "open_count",
            "created_at",
        ]
        read_only_fields = ["created_at"]
        validators = []  # name + side uniqueness is checked (with a clearer message) in validate()

    def validate(self, attrs):
        kind = attrs.get("kind") or (self.instance.kind if self.instance else "development")
        kind_changed = bool(self.instance) and kind != self.instance.kind

        # The same name may exist once per side (support / development): those two are
        # twins, and support can escalate tasks to the developers of its twin.
        name = attrs.get("name") or (self.instance.name if self.instance else "")
        if name and ("name" in attrs or kind_changed):
            clash = Project.objects.filter(name__iexact=name, kind=kind)
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                label = "support" if kind == "support" else "development"
                raise serializers.ValidationError(
                    {"name": f"A {label} project with that name already exists."}
                )
        if self.instance and "name" in attrs and attrs["name"].lower() != self.instance.name.lower():
            twin = self.instance.twin()  # renaming keeps the pair together (see the view)
            if twin and Project.objects.filter(name__iexact=attrs["name"], kind=twin.kind).exclude(pk=twin.pk).exists():
                raise serializers.ValidationError(
                    {"name": f"Its {twin.kind} twin can't take that name: another {twin.kind} project already uses it."}
                )

        if kind_changed and self.instance.complaints.exists():
            raise serializers.ValidationError(
                {"kind": "This project already has items, so its type can't change."}
            )
        # Support projects hold support staff, development projects hold developers.
        if "members" in attrs or kind_changed:
            members = (
                attrs["members"]
                if "members" in attrs
                else list(self.instance.members.all())
            )
            side = ("support", "both") if kind == "support" else ("developer", "both")
            wrong = [m.full_name for m in members if m.access not in side]
            if wrong:
                label = "Support" if kind == "support" else "Development"
                raise serializers.ValidationError(
                    {"members": f"{label} projects can't include: {', '.join(wrong)}."}
                )
        return attrs

    def validate_name(self, name):
        name = name.strip()
        if not name:
            raise serializers.ValidationError("Name is required.")
        return name  # uniqueness is checked per side in validate()


class SectionSerializer(SectionOwnersMixin, serializers.ModelSerializer):
    owners = serializers.PrimaryKeyRelatedField(
        many=True, queryset=TeamMember.objects.all(), required=False
    )
    project_name = serializers.CharField(source="project.name", read_only=True)
    task_count = serializers.IntegerField(read_only=True)
    open_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Section
        fields = [
            "id",
            "project",
            "project_name",
            "name",
            "order",
            "owners",
            "owner_names",
            "is_mine",
            "task_count",
            "open_count",
            "created_by_name",
            "created_at",
        ]
        read_only_fields = ["order", "created_by_name", "created_at"]

    def validate_name(self, name):
        name = name.strip()
        if not name:
            raise serializers.ValidationError("Section name is required.")
        return name

    def validate(self, attrs):
        project = attrs.get("project") or (self.instance.project if self.instance else None)
        if project is None:
            raise serializers.ValidationError({"project": "Choose a project."})
        if self.instance and project.id != self.instance.project_id:
            raise serializers.ValidationError({"project": "A section can't move to another project."})
        if project.kind != "development":
            raise serializers.ValidationError(
                {"project": "Sections are only for development projects."}
            )
        name = attrs.get("name") or (self.instance.name if self.instance else "")
        dupes = Section.objects.filter(project=project, name__iexact=name)
        if self.instance:
            dupes = dupes.exclude(pk=self.instance.pk)
        if dupes.exists():
            raise serializers.ValidationError(
                {"name": "This project already has a section with that name."}
            )
        owners = attrs.get("owners")
        if owners:
            member_ids = set(project.members.values_list("id", flat=True))
            bad = [
                m.full_name
                for m in owners
                if m.id not in member_ids or m.access not in ("developer", "both")
            ]
            if bad:
                raise serializers.ValidationError(
                    {"owners": f"Not a developer in this project: {', '.join(bad)}"}
                )
        return attrs

    def create(self, validated_data):
        last = (
            Section.objects.filter(project=validated_data["project"]).order_by("-order").first()
        )
        validated_data["order"] = (last.order + 1) if last else 0
        return super().create(validated_data)


class SprintSerializer(serializers.ModelSerializer):
    # The projects that take part in the sprint (development projects only).
    projects = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Project.objects.all(), required=False
    )
    # Old single-project input, still accepted: {"project": 3} means projects = [3].
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), required=False, allow_null=True, write_only=True
    )
    project_names = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    # The people who work in the sprint (team member ids). Only they and the admin see it.
    members = serializers.PrimaryKeyRelatedField(
        many=True, queryset=TeamMember.objects.all(), required=False
    )
    member_names = serializers.SerializerMethodField()
    total_count = serializers.SerializerMethodField()
    done_count = serializers.SerializerMethodField()
    open_count = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    closing_state = serializers.SerializerMethodField()
    unfinished_sections = serializers.SerializerMethodField()

    class Meta:
        model = Sprint
        fields = [
            "id",
            "projects",
            "project",
            "project_names",
            "project_name",
            "members",
            "member_names",
            "name",
            "goal",
            "status",
            "start_date",
            "end_date",
            "total_count",
            "done_count",
            "open_count",
            "auto_close",
            "days_left",
            "closing_state",
            "unfinished_sections",
            "unfinished_at_close",
            "completed_by_name",
            "created_by_name",
            "created_at",
            "completed_at",
        ]
        read_only_fields = [
            "status",
            "created_by_name",
            "created_at",
            "completed_at",
            "completed_by_name",
            "unfinished_at_close",
        ]

    def _visible_projects(self, obj):
        """Everyone sees the sprint's projects they belong to; the admin sees all."""
        request = self.context.get("request")
        projects = list(obj.projects.all())
        if request is None or request.user.is_superuser:
            return projects
        uid = request.user.id
        return [p for p in projects if any(m.user_id == uid for m in p.members.all())]

    def get_project_names(self, obj):
        return [p.name for p in self._visible_projects(obj)]

    def get_project_name(self, obj):
        return ", ".join(self.get_project_names(obj))

    def get_member_names(self, obj):
        return [m.full_name for m in obj.members.all()]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        visible = [p.id for p in self._visible_projects(instance)]
        data["projects"] = visible
        data["project"] = visible[0] if visible else None  # old single-project field
        return data

    # The view annotates total_count / done_count; fall back to queries otherwise.
    def get_total_count(self, obj):
        if hasattr(obj, "total_count"):
            return obj.total_count
        return obj.complaints.count()

    def get_done_count(self, obj):
        if hasattr(obj, "done_count"):
            return obj.done_count
        closed = Status.objects.filter(is_closed=True).values_list("value", flat=True)
        return obj.complaints.filter(status__in=list(closed)).count()

    def get_open_count(self, obj):
        return self.get_total_count(obj) - self.get_done_count(obj)

    # Days until the end date (negative = past it). Only for an active sprint.
    def get_days_left(self, obj):
        if obj.status != "active" or not obj.end_date:
            return None
        return (obj.end_date - timezone.localdate()).days

    # planned | on_track | ready_to_close | closing_soon | overdue | closed | closed_incomplete
    # "closing_soon" / "overdue" mean the sprint is about to close (or already should)
    # while tasks are still open, so the page shows a warning.
    def get_closing_state(self, obj):
        if obj.status == "planned":
            return "planned"
        if obj.status == "completed":
            return "closed_incomplete" if obj.unfinished_at_close else "closed"
        if self.get_open_count(obj) == 0 and self.get_total_count(obj) > 0:
            return "ready_to_close"
        days = self.get_days_left(obj)
        if days is None:
            return "on_track"
        if days < 0:
            return "overdue"
        return "closing_soon" if days <= 2 else "on_track"

    # Sections (of the sprint's tasks) that still have open work, for the warning.
    def get_unfinished_sections(self, obj):
        if obj.status != "active":
            return []
        closed = list(Status.objects.filter(is_closed=True).values_list("value", flat=True))
        rows = (
            obj.complaints.exclude(status__in=closed)
            .values("section_id", "section__name")
            .annotate(open_count=Count("id"))
            .order_by("section__name")
        )
        return [
            {
                "id": r["section_id"],
                "name": r["section__name"] or "No section",
                "open_count": r["open_count"],
            }
            for r in rows
        ]

    def validate_name(self, name):
        name = name.strip()
        if not name:
            raise serializers.ValidationError("Sprint name is required.")
        return name

    def validate(self, attrs):
        legacy = attrs.pop("project", None)
        if "projects" not in attrs and legacy is not None:
            attrs["projects"] = [legacy]

        if "projects" in attrs:
            projects = list(attrs["projects"])
        elif self.instance:
            projects = list(self.instance.projects.all())
        else:
            projects = []
        if not projects:
            raise serializers.ValidationError({"projects": "Add at least one project to the sprint."})
        wrong = [p.name for p in projects if p.kind != "development"]
        if wrong:
            raise serializers.ValidationError(
                {"projects": f"Sprints are only for development projects: {', '.join(wrong)}."}
            )

        # Only developers of the sprint's own projects can work in it.
        if "members" in attrs:
            allowed = set(
                TeamMember.objects.filter(
                    projects__in=projects, access__in=("developer", "both")
                ).values_list("id", flat=True)
            )
            bad = [m.full_name for m in attrs["members"] if m.id not in allowed]
            if bad:
                raise serializers.ValidationError(
                    {"members": f"Not a developer in this sprint's projects: {', '.join(bad)}."}
                )

        name = attrs.get("name") or (self.instance.name if self.instance else "")
        dupes = Sprint.objects.filter(projects__in=projects, name__iexact=name).distinct()
        if self.instance:
            dupes = dupes.exclude(pk=self.instance.pk)
        if dupes.exists():
            raise serializers.ValidationError(
                {"name": "One of these projects already has a sprint with that name."}
            )

        start = attrs.get("start_date", self.instance.start_date if self.instance else None)
        end = attrs.get("end_date", self.instance.end_date if self.instance else None)
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "End date can't be before the start date."})
        return attrs