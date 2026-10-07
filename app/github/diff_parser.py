def parse_diff(diff: str):
    files = []

    sections = diff.split("diff --git ")

    for section in sections[1:]:
        lines = section.splitlines()

        first_line = lines[0]
        parts = first_line.split()

        if len(parts) < 2:
            continue

        old_file = parts[0][2:]
        new_file = parts[1][2:]
        if any(line.startswith("Binary files") for line in lines):
            files.append({
                "filename": new_file,
                "status": "binary",
                "patch": "",
                "additions": 0,
                "deletions": 0
            })
            continue
        patch_lines = []
        additions = 0
        deletions = 0

        for line in lines[1:]:
            if line.startswith("@@"):
                patch_lines.append(line)

            elif line.startswith("+") and not line.startswith("+++"):
                patch_lines.append(line)
                additions += 1

            elif line.startswith("-") and not line.startswith("---"):
                patch_lines.append(line)
                deletions += 1

            elif (
                not line.startswith("diff --git")
                and not line.startswith("index ")
                and not line.startswith("---")
                and not line.startswith("+++")
            ):
                patch_lines.append(line)

        if old_file == new_file:
            status = "modified"
            filename = new_file
        elif old_file == "/dev/null":
            status = "added"
            filename = new_file
        elif new_file == "/dev/null":
            status = "deleted"
            filename = old_file
        else:
            status = "renamed"
            filename = new_file

        files.append({
            "filename": filename,
            "status": status,
            "patch": "\n".join(patch_lines),
            "additions": additions,
            "deletions": deletions
        })

    return files

def parse_review(review: str):
    if not review:
        return {
            "status": "error",
            "message": "No response from LLM"
        }

    if review.strip().upper() == "NO ISSUES":
        return {
            "status": "clean",
            "issues": []
        }

    issues = []

    for block in review.split("---"):
        block = block.strip()

        if not block:
            continue

        issue = {}

        for line in block.splitlines():
            line = line.strip()

            if line.startswith("Severity:"):
                issue["severity"] = line.split(":", 1)[1].strip()

            elif line.startswith("Line:"):
                issue["line"] = line.split(":", 1)[1].strip()

            elif line.startswith("Title:"):
                issue["title"] = line.split(":", 1)[1].strip()

            elif line.startswith("Description:"):
                issue["description"] = line.split(":", 1)[1].strip()

            elif line.startswith("Fix:"):
                issue["fix"] = line.split(":", 1)[1].strip()

        if issue:
            issues.append(issue)

    return {
        "status": "reviewed",
        "issues": issues
    }