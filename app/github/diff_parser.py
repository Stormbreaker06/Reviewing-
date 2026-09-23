def parse_diff(diff: str):
    files = []

    sections = diff.split("diff --git ")

    for section in sections[1:]:
        lines = section.splitlines()

        first_line = lines[0]
        parts = first_line.split(" ")

        old_file = parts[0][2:]
        new_file = parts[1][2:]

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