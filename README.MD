# AI Pull Request Review Agent

## Workflow Design

Before implementing the LangGraph workflow, the data flow between each node was designed to determine the shared state (`ReviewState`) and the responsibilities of each node.

| Node                | Inputs                                                                                       | Outputs                                                           |
| ------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Fetch PR            | `pr_number`, `repo_name`, `github_token`                                                     | `raw_diff`, `changed_files`, `commit_messages`, `pr_metadata`     |
| Context             | `raw_diff`, `changed_files`, `commit_messages`, `pr_metadata`                                | `parsed_diff`, `file_summaries`, `pr_intent`, `language_detected` |
| Review (Parallel)   | `parsed_diff`, `file_summaries`, `pr_intent`                                                 | `review_comments`, `code_quality_score`, `suggestions`            |
| Security (Parallel) | `parsed_diff`, `file_summaries`, `language_detected`                                         | `security_issues`, `severity_levels`, `vulnerable_lines`          |
| Summary             | `review_comments`, `code_quality_score`, `suggestions`, `security_issues`, `severity_levels` | `final_summary`, `overall_verdict`, `formatted_comment`           |
| Post Review         | `formatted_comment`, `overall_verdict`, `pr_number`, `github_token`                          | `posted_comment_url`, `review_status`                             |

## Planned LangGraph Workflow

```text
START
   │
Webhook
   │
Fetch PR
   │
Context
   │
 ┌─┴───────────┐
 │             │
Review     Security
 │             │
 └─────┬───────┘
       │
    Summary
       │
  Post Review
       │
      END
```
