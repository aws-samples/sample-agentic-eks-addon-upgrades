# Cost

This page explains what you pay for, what drives the cost of a run, and how the sample keeps model
cost down. For an estimate, use the [AWS Pricing Calculator](https://calculator.aws/) with your
own numbers.

## In short

* **You pay per run.** There are no always-on instances, NAT gateways or provisioned capacity.
* **Model tokens are the largest cost**, and they depend mostly on how many components need
  research.
* **The first run costs the most**, because the fact store starts empty. Later runs reuse
  approved facts.
* **An idle deployment costs very little:** stored data, plus one KMS key at about $1 a month.

## What drives the cost of a run

| Service | What uses it | What makes it cost more | Pricing |
|---|---|---|---|
| Amazon Bedrock and Bedrock AgentCore | The research, brief and upgrade agents | More components to research, longer source pages and release notes, more version hops to brief | [Bedrock](https://aws.amazon.com/bedrock/pricing/), [AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/) |
| AWS CodeBuild | Upgrade lanes, only when the upgrade stage is on | More approved steps, and longer health checks (`upgrade_health_deadline_seconds`) | [CodeBuild](https://aws.amazon.com/codebuild/pricing/) |
| AWS Lambda | Every stage | More runs. Research and brief calls take the longest | [Lambda](https://aws.amazon.com/lambda/pricing/) |
| AWS Step Functions | Every step of the workflow | More runs. The research loop and the release-gate polling add steps | [Step Functions](https://aws.amazon.com/step-functions/pricing/) |
| Amazon S3 | Stage outputs, fetched sources and reports | More runs. Old versions expire after 90 days | [S3](https://aws.amazon.com/s3/pricing/) |
| Amazon DynamoDB | Facts and approval tokens | More runs. On-demand capacity | [DynamoDB](https://aws.amazon.com/dynamodb/pricing/) |
| AWS KMS | The key that encrypts CloudWatch Logs | About $1 a month, plus requests | [KMS](https://aws.amazon.com/kms/pricing/) |
| Amazon SES | Approval and report emails | Small | [SES](https://aws.amazon.com/ses/pricing/) |
| Amazon CloudWatch Logs | Logs from every stage and lane | Small. Kept for 30 days | [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) |
| AWS Secrets Manager | The optional GitHub token | One secret, if you use it | [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/) |

## How model cost is kept down

* **Facts are shared across clusters.** A compatibility fact is true for every cluster, so a
  component researched for one cluster is not researched again for the next.
* **Only real gaps reach a model.** `Resolve` sends a component to research only when no approved
  fact answers the question.
* **Briefs are cached.** A brief for the same upgrade step is reused for 7 days.
* **Readiness-only runs skip the upgrade stage**, so they use no CodeBuild and no upgrade agent.
